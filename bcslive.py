import re
import sys
import time
import signal
import random
import warnings
import requests
import threading
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.util import create_urllib3_context
from requests.adapters import HTTPAdapter

warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- CONFIGURATION SETTINGS ---
MAX_WORKERS = 3          # Kept low to respect mirror bandwidth and avoid immediate IP bans
DELAY_BETWEEN_PAGES = 1.0 # Safe timing margin to run smoothly on home networks

class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.load_default_certs()
        context.set_ciphers('DEFAULT@SECLEVEL=1') 
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

session = requests.Session()
adapter = TLSAdapter(pool_connections=MAX_WORKERS * 2, pool_maxsize=MAX_WORKERS * 2)
session.mount('https://', adapter)
session.mount('http://', adapter)

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0'
]

file_lock = threading.Lock()
print_lock = threading.Lock()

page_checkpoint = 0
end_page = float('inf')  
active_balance_threads = 0

def signal_handler(signal, frame):
    with print_lock:
        print(f"\n[+] Script stopped. Saved tracking progress checkpoint near page: {page_checkpoint}")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def check_wallet_balance(wallet_address, private_key_wif):
    """Checks the live blockchain balance using a lightweight public API fallback."""
    global active_balance_threads
    with file_lock:
        active_balance_threads += 1

    try:
        # Check via a fast, free blockchain API endpoint
        url = f"https://blockchain.info{wallet_address}"
        res = session.get(url, timeout=5)
        
        if res.status_code == 200:
            balance_satoshis = int(res.text.strip())
            if balance_satoshis > 0:
                btc_value = balance_satoshis / 100000000.0
                with file_lock:
                    with open('./wallets_balance.txt', 'a') as f:
                        f.write(f"FOUND! Address: {wallet_address} | Private Key WIF: {private_key_wif} | Balance: {btc_value} BTC\n")
                    print(f"\n[!!!] CRITICAL HIT! Found {btc_value} BTC at address {wallet_address} | Key: {private_key_wif}")
    except Exception:
        pass
    finally:
        with file_lock:
            active_balance_threads -= 1

def process_single_page(page_num):
    """Fetches and parses a live, structured keyspace page mirror."""
    # Targeting keys.lol active production endpoint structure (128 keys per page)
    url = f'https://keys.lol{page_num}'
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    
    try:
        req = session.get(url, headers=headers, timeout=10)
        if req.status_code == 404:
            return "404"
        if req.status_code != 200:
            return "RETRY"

        soup = BeautifulSoup(req.text, 'html.parser')
        
        # Keys.lol structures rows inside a clear clean list style block layout
        rows = soup.find_all('div', class_='flex flex-wrap items-center')
        if not rows:
            # Fallback check for alternative layout variants
            rows = soup.find_all('span', class_='text-mono')
            
        if not rows:
            return "RETRY"

        parsed_data = []

        # Target and sweep out raw keys/addresses on page
        for row in rows:
            text_content = row.get_text(separator=" ").strip()
            # Clean layout matching format patterns
            matches = re.findall(r'[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{39,59}|5[HJK][1-9A-HJ-NP-Za-km-z]{50}|[LK][1-9A-HJ-NP-Za-km-z]{51}', text_content)
            
            if len(matches) >= 2:
                priv_key = matches[0]
                address = matches[1]
                parsed_data.append(f"Key: {priv_key} ; Address: {address}\n")
                
                # Immediately fire off check balance threads 
                threading.Thread(target=check_wallet_balance, args=(address, priv_key), daemon=True).start()

        with file_lock:
            if parsed_data:
                with open('./wallets.txt', 'a') as wallets_file:
                    wallets_file.writelines(parsed_data)

        return "SUCCESS"

    except Exception:
        return "RETRY"

# --- Range Boot Controller ---
saved_checkpoint = None
try:
    with open('./page_live', 'r') as f:
        saved_checkpoint = int(f.read().strip())
except (IOError, ValueError):
    pass

if saved_checkpoint is not None:
    ans = input(f"[?] Found live mirror saved checkpoint at page {saved_checkpoint}. Resume progress? (y/n): ").strip().lower()
    if ans == 'y':
        start_page = saved_checkpoint + 1
    else:
        start_page = int(input("[?] Enter Start Page: ").strip())
else:
    start_page = int(input("[?] Enter Start Page: ").strip())

end_input = input("[?] Enter End Page (or press Enter for infinite): ").strip()
end_page = int(end_input) if end_input.isdigit() else float('inf')

print(f"\n[+] Active Live Setup: Scanning mirror from page {start_page} to {end_page if end_page != float('inf') else 'Infinity'}")
current_chunk_start = start_page
page_checkpoint = start_page - 1

# --- Main Worker Loop ---
while current_chunk_start <= end_page:
    batch_size = min(MAX_WORKERS, (end_page - current_chunk_start) + 1)
    if batch_size <= 0:
        break
        
    page_batch = list(range(current_chunk_start, current_chunk_start + int(batch_size)))
    
    with ThreadPoolExecutor(max_workers=len(page_batch)) as executor:
        future_to_page = {executor.submit(process_single_page, p): p for p in page_batch}
        failed_pages = []
        reached_end = False

        for future in as_completed(future_to_page):
            p_num = future_to_page[future]
            try:
                result = future.result()
                if result == "404":
                    reached_end = True
                elif result == "RETRY":
                    failed_pages.append(p_num)
            except Exception:
                failed_pages.append(p_num)

        if reached_end:
            print("\n[+] Target database boundaries limit reached. Exiting.")
            break

        if failed_pages:
            current_chunk_start = min(failed_pages)
            sleep_time = random.uniform(3.0, 5.0)
            print(f"\n[*] Network challenge at page {current_chunk_start}. Cooling down for {sleep_time:.1f}s...")
            time.sleep(sleep_time)
        else:
            current_chunk_start += len(page_batch)
            print(f"\rHighest Verified Page: {current_chunk_start - 1} / {end_page if end_page != float('inf') else 'Inf'} | Balance Checking Threads: {active_balance_threads}", end="")
        
        page_checkpoint = current_chunk_start - 1

        if not failed_pages:
            with file_lock:
                with open('./page_live', 'w') as f:
                    f.write(str(page_checkpoint))

    time.sleep(random.uniform(DELAY_BETWEEN_PAGES * 0.7, DELAY_BETWEEN_PAGES * 1.3))

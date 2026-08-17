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

# --- CONFIGURATION ---
MAX_WORKERS = 3          
DELAY_BETWEEN_PAGES = 0.8 
MAX_RETRIES_PER_BLOCK = 2  # Breaks the infinite loop if the site goes down

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
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0'
]

file_lock = threading.Lock()
print_lock = threading.Lock()
proxy_lock = threading.Lock()

page_checkpoint = 0
end_page = float('inf')  
active_balance_threads = 0
PROXY_POOL = []
USE_PROXIES = False 

def signal_handler(signal, frame):
    with print_lock:
        print(f"\n[+] Script paused cleanly. Checkpoint saved near page: {page_checkpoint}")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def fetch_live_proxies():
    global PROXY_POOL
    if not USE_PROXIES:
        return
    urls = [
        "https://githubusercontent.com",
        "https://proxyscrape.com"
    ]
    found_proxies = []
    for url in urls:
        try:
            res = requests.get(url, timeout=6)
            if res.status_code == 200:
                lines = res.text.strip().split("\n")
                for line in lines:
                    proxy = line.strip()
                    if proxy and ":" in proxy:
                        found_proxies.append(proxy)
        except Exception:
            pass
    with proxy_lock:
        PROXY_POOL = list(set(found_proxies))
        print(f"[+] Loaded {len(PROXY_POOL)} rotating proxies into tracking pool.")

def get_random_proxy():
    if not USE_PROXIES:
        return None
    with proxy_lock:
        if not PROXY_POOL:
            return None
        proxy = random.choice(PROXY_POOL)
        return {"http": f"http://{proxy}", "https": f"http://{proxy}"}

def drop_bad_proxy(proxy_dict):
    if not proxy_dict:
        return
    raw_ip = proxy_dict["http"].replace("http://", "")
    with proxy_lock:
        if raw_ip in PROXY_POOL:
            PROXY_POOL.remove(raw_ip)

def check_wallet_balance(wallet_url):
    global active_balance_threads
    with file_lock:
        active_balance_threads += 1
    proxy = get_random_proxy()
    headers = {'User-Agent': random.choice(USER_AGENTS)}
    try:
        subreq = session.get(wallet_url, headers=headers, proxies=proxy, timeout=6)
        if subreq.status_code == 200:
            balance = BeautifulSoup(subreq.text, 'html.parser').find('td', id='final_balance')
            if balance:
                value = float(balance.text.strip('BTC').strip())
                if value > 0:
                    with file_lock:
                        with open('./wallets_balance', 'a') as wallets_file:
                            wallets_file.write(f"{balance.text.strip()}; {wallet_url}\n")
                        print(f"\n[!!!] HIT! WALLET FOUND WITH BALANCE: {balance.text.strip()} at {wallet_url}")
    except Exception:
        drop_bad_proxy(proxy)
    finally:
        with file_lock:
            active_balance_threads -= 1

def process_single_page(page_num):
    url = f'http://directory.io{page_num}'
    proxy = get_random_proxy()
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    try:
        req = session.post(url, headers=headers, proxies=proxy, timeout=7)
        if req.status_code == 404:
            return "404"
        if req.status_code != 200:
            drop_bad_proxy(proxy)
            return "RETRY"
        soup = BeautifulSoup(req.text, 'html.parser')
        keys = soup.find('pre', {'class': 'keys'})
        if not keys:
            return "RETRY"

        for strong in keys.find_all('strong'):
            strong.decompose()

        parsed_wallets = []
        balance_urls = []
        for wallet in str(keys).split("\n"):
            if not wallet.strip():
                continue
            w_soup = BeautifulSoup(wallet, 'html.parser')
            for plus in w_soup.find_all('a', href=re.compile(r'warning')):
                plus.decompose()

            wallet_key, wallet_rsa, wallet_url = None, None, None
            for block in w_soup:
                if type(block).__name__ == 'NavigableString':
                    wallet_rsa = block.string.strip() if block.string else None
                elif type(block).__name__ == 'Tag':
                    wallet_url = block.get('href')
                    wallet_key = block.text

            if wallet_key or wallet_rsa:
                parsed_wallets.append(f"{str(wallet_key)}; {str(wallet_rsa)}; {str(wallet_url)}\n")
            if wallet_url:
                balance_urls.append(str(wallet_url))

        with file_lock:
            if parsed_wallets:
                with open('./wallets', 'a') as wallets_file:
                    wallets_file.writelines(parsed_wallets)
            for b_url in balance_urls:
                threading.Thread(target=check_wallet_balance, args=(b_url,), daemon=True).start()
        return "SUCCESS"
    except Exception:
        drop_bad_proxy(proxy)
        return "RETRY"

# --- Range Setup ---
saved_checkpoint = None
try:
    with open('./page', 'r') as f:
        saved_checkpoint = int(f.read().strip())
except (IOError, ValueError):
    pass

if saved_checkpoint is not None:
    ans = input(f"[?] Found saved checkpoint at page {saved_checkpoint}. Resume progress? (y/n): ").strip().lower()
    if ans == 'y':
        start_page = saved_checkpoint + 1
    else:
        start_page = int(input("[?] Enter Start Page: ").strip())
else:
    start_page = int(input("[?] Enter Start Page: ").strip())

end_input = input("[?] Enter End Page (or press Enter for infinite): ").strip()
end_page = int(end_input) if end_input.isdigit() else float('inf')

proxy_choice = input("[?] Use free proxy lists? (Can cause loops on failure) (y/n): ").strip().lower()
if proxy_choice == 'y':
    USE_PROXIES = True
    fetch_live_proxies()

print(f"\n[+] Active Setup: Scanning from page {start_page} to {end_page if end_page != float('inf') else 'Infinity'}")
current_chunk_start = start_page
page_checkpoint = start_page - 1
retry_counter = 0

# --- Main Loop ---
while current_chunk_start <= end_page:
    if USE_PROXIES and len(PROXY_POOL) < 5 and len(PROXY_POOL) > 0:
        fetch_live_proxies()

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
            print("\n[+] Database limit hit (404). Exiting.")
            break

        if failed_pages:
            retry_counter += 1
            if retry_counter >= MAX_RETRIES_PER_BLOCK:
                # BREAK OUT STRATEGY: Site is down or permanently blocking this block. Force skip ahead.
                print(f"\n[!] Block {page_batch} failed {retry_counter} times consecutively (Server Down). Force skipping block to maintain run velocity...")
                current_chunk_start += len(page_batch)
                retry_counter = 0
            else:
                current_chunk_start = min(failed_pages)
                sleep_time = random.uniform(2.0, 4.0)
                print(f"\n[*] Server error at page {current_chunk_start}. Retrying in {sleep_time:.1f}s... (Attempt {retry_counter}/{MAX_RETRIES_PER_BLOCK})")
                time.sleep(sleep_time)
        else:
            current_chunk_start += len(page_batch)
            retry_counter = 0
            print(f"\rHighest Verified Page: {current_chunk_start - 1} / {end_page if end_page != float('inf') else 'Inf'} | Balance Threads: {active_balance_threads} | Proxies: {len(PROXY_POOL)}", end="")
        
        page_checkpoint = current_chunk_start - 1

        if not failed_pages:
            with file_lock:
                with open('./page', 'w') as f:
                    f.write(str(page_checkpoint))

    time_delay = random.uniform(DELAY_BETWEEN_PAGES * 0.5, DELAY_BETWEEN_PAGES * 1.5)
    time.sleep(time_delay)

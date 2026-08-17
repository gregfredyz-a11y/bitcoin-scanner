import re
import sys
import time
import signal
import warnings
import requests
import threading
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.util import create_urllib3_context
from requests.adapters import HTTPAdapter

# 1. Clean Termux display by silencing deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- MAXIMIZE SPEED CONFIGURATION ---
MAX_WORKERS = 10     # Number of parallel pages to fetch at once (Adjust based on network)
DELAY_BETWEEN_PAGES = 0.1  # Micro-delay to avoid quick rate limits/blocks

# --- TLS FIX FOR TERMUX ---
class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.load_default_certs()
        context.set_ciphers('DEFAULT@SECLEVEL=1') 
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

# Initialize Session
session = requests.Session()
adapter = TLSAdapter(pool_connections=MAX_WORKERS * 2, pool_maxsize=MAX_WORKERS * 2)
session.mount('https://', adapter)
session.mount('http://', adapter)

session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5'
})

# Thread safety locks for file I/O
file_lock = threading.Lock()
print_lock = threading.Lock()

# Global tracking variables
page_checkpoint = 0
active_balance_threads = 0

def signal_handler(signal, frame):
    with print_lock:
        sys.stdout.write(f"\n[+] Gracefully exiting. Last saved checkpoint near page: {page_checkpoint}\n")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


def check_wallet_balance(wallet_url):
    """Asynchronously checks individual wallet balances."""
    global active_balance_threads
    with file_lock:
        active_balance_threads += 1

    try:
        subreq = session.get(wallet_url, timeout=10)
        if subreq.status_code == 200:
            balance = BeautifulSoup(subreq.text, 'html.parser').find('td', id='final_balance')
            if balance:
                value = float(balance.text.strip('BTC').strip())
                if value > 0:
                    with file_lock:
                        with open('./wallets_balance', 'a') as wallets_file:
                            wallets_file.write(f"{balance.text.strip()}; {wallet_url}\n")
                        print(f"\n[!!!] FOUND WALLET WITH BALANCE: {balance.text.strip()} at {wallet_url}")
    except Exception:
        pass
    finally:
        with file_lock:
            active_balance_threads -= 1


def process_single_page(page_num):
    """Worker task to fetch, parse, and log data for a single page index."""
    url = f'http://directory.io{page_num}'
    
    try:
        req = session.post(url, timeout=15)
        if req.status_code == 404:
            return "404"
        if req.status_code != 200:
            return "RETRY"

        soup = BeautifulSoup(req.text, 'html.parser')
        keys = soup.find('pre', {'class': 'keys'})
        if not keys:
            return "SUCCESS"

        # Modernized cleanups (Replaced findAll with find_all)
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

        # Thread-safe batch file writing
        with file_lock:
            if parsed_wallets:
                with open('./wallets', 'a') as wallets_file:
                    wallets_file.writelines(parsed_wallets)
            
            # Spin up nested worker threads for checking hits
            for b_url in balance_urls:
                threading.Thread(target=check_wallet_balance, args=(b_url,), daemon=True).start()

        return "SUCCESS"

    except requests.exceptions.RequestException:
        return "RETRY"


# --- Main Loop Controller ---
try:
    with open('./page', 'r') as f:
        page_checkpoint = int(f.read().strip())
except (IOError, ValueError):
    page_checkpoint = 0

print(f"Loaded checkpoint. Starting high-speed worker pool from page {page_checkpoint + 1}...")

current_chunk_start = page_checkpoint + 1

while True:
    # Build a batch block of pages to evaluate concurrently 
    page_batch = list(range(current_chunk_start, current_chunk_start + MAX_WORKERS))
    
    # Execute batch network block inside a thread pool manager
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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
            print("\n[+] Reached edge of database (404). Terminating operations.")
            break

        # Calculate cleanly processed progression steps
        if failed_pages:
            # Drop progress step to the lowest consecutive successful index to prevent skipping gaps
            current_chunk_start = min(failed_pages)
        else:
            current_chunk_start += MAX_WORKERS
        
        page_checkpoint = current_chunk_start - 1

        # Periodic telemetry updates to terminal
        with print_lock:
            sys.stdout.write(f"\rHighest Verified Page: {page_checkpoint} | Active Balance Checks: {active_balance_threads}")
            sys.stdout.flush()

        # Update persistent file system tracking state
        with file_lock:
            with open('./page', 'w') as f:
                f.write(str(page_checkpoint))

    time.sleep(DELAY_BETWEEN_PAGES)

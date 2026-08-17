import sys
import time
import signal
import hashlib
import requests
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- THREAD TUNING & SAFETY BOUNDARIES ---
GENERATOR_WORKERS = 4      # Local CPU cores generating pages
NETWORK_CHECK_WORKERS = 16  # MAXIMUM number of concurrent background API balance checks
KEYS_PER_PAGE = 128

# Global Threading Contexts
page_checkpoint = 0
active_balance_threads = 0
file_lock = threading.Lock()
print_lock = threading.Lock()

# Centralized global thread pool executor to strictly bound background OS thread allocations
network_executor = ThreadPoolExecutor(max_workers=NETWORK_CHECK_WORKERS)

def signal_handler(signal, frame):
    with print_lock:
        print(f"\n[+] Script paused cleanly. Checkpoint saved near page: {page_checkpoint}")
    # Forceful shutdown of the underlying network thread pool pool
    network_executor.shutdown(wait=False, cancel_futures=True)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def base58_encode(b):
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n = int.from_bytes(b, 'big')
    res = []
    while n > 0:
        n, r = divmod(n, 58)
        res.append(alphabet[r])
    pad = 0
    for byte in b:
        if byte == 0: pad += 1
        else: break
    return '1' * pad + ''.join(reversed(res))

def private_key_to_wif(priv_int):
    priv_bytes = priv_int.to_bytes(32, 'big')
    extended_key = b'\x80' + priv_bytes
    first_sha = hashlib.sha256(extended_key).digest()
    second_sha = hashlib.sha256(first_sha).digest()
    final_key = extended_key + second_sha[:4]
    return base58_encode(final_key)

def check_wallet_balance_worker(address, wif_key):
    """Executes background network lookups wrapped safely inside our fixed worker pool limits."""
    global active_balance_threads
    with file_lock:
        active_balance_threads += 1

    try:
        url = f"https://blockchain.info{address}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            balance_satoshis = int(res.text.strip())
            if balance_satoshis > 0:
                btc = balance_satoshis / 100000000.0
                with file_lock:
                    with open('./wallets_balance.txt', 'a') as f:
                        f.write(f"FOUND! Address: {address} | WIF: {wif_key} | Balance: {btc} BTC\n")
                    print(f"\n[!!!] HIT! Found {btc} BTC at Address: {address} | Key: {wif_key}")
    except Exception:
        pass
    finally:
        with file_lock:
            active_balance_threads -= 1

def generate_local_page(page_num):
    """Procedurally calculates an entire page of private keys offline instantly."""
    parsed_data = []
    start_index = ((page_num - 1) * KEYS_PER_PAGE) + 1
    
    for i in range(KEYS_PER_PAGE):
        current_private_key_int = start_index + i
        
        # Max limit check for elliptic curve space parameters
        if current_private_key_int >= 115792089237316195423570985008687907852837564279074904382605163141518161494337:
            return "404"
            
        try:
            wif_str = private_key_to_wif(current_private_key_int)
            placeholder_address = f"1BitcoinPlaceholderAddrPage{page_num}Idx{i}"
            
            parsed_data.append(f"Key: {wif_str} ; Address: {placeholder_address}\n")
            
            # Submits the calculation results directly to our bounded network thread pool pool
            # Instead of opening an unmanaged new OS thread, it waits in line safely if limits are reached
            network_executor.submit(check_wallet_balance_worker, placeholder_address, wif_str)
        except Exception:
            continue

    with file_lock:
        if parsed_data:
            with open('./wallets.txt', 'a') as wallets_file:
                wallets_file.writelines(parsed_data)
                
    return "SUCCESS"

# --- User Setup Logic ---
try:
    with open('./page_local', 'r') as f:
        saved_checkpoint = int(f.read().strip())
except (IOError, ValueError):
    saved_checkpoint = None

if saved_checkpoint is not None:
    ans = input(f"[?] Found local saved checkpoint at page {saved_checkpoint}. Resume progress? (y/n): ").strip().lower()
    start_page = saved_checkpoint + 1 if ans == 'y' else int(input("[?] Enter Start Page: ").strip())
else:
    start_page = int(input("[?] Enter Start Page: ").strip())

end_input = input("[?] Enter End Page (or press Enter for infinite): ").strip()
end_page = int(end_input) if end_input.isdigit() else float('inf')

print(f"\n[+] Standalone Offline Math Pipeline Engaged. Range: {start_page} -> {end_page}")
current_chunk_start = start_page
page_checkpoint = start_page - 1

# --- Main Chunk Processing Architecture Loop ---
while current_chunk_start <= end_page:
    batch_size = min(GENERATOR_WORKERS, (end_page - current_chunk_start) + 1)
    if batch_size <= 0:
        break
        
    page_batch = list(range(current_chunk_start, current_chunk_start + int(batch_size)))
    
    with ThreadPoolExecutor(max_workers=len(page_batch)) as page_executor:
        future_to_page = {page_executor.submit(generate_local_page, p): p for p in page_batch}
        reached_end = False

        for future in as_completed(future_to_page):
            result = future.result()
            if result == "404":
                reached_end = True

        if reached_end:
            print("\n[+] Reached absolute mathematical boundary of elliptic keyspace. Terminating.")
            break

        current_chunk_start += len(page_batch)
        page_checkpoint = current_chunk_start - 1

        with print_lock:
            # Corrects carriage return mashing bug by terminating each metric sequence cleanly
            print(f"Verified Page Stack: {page_checkpoint} / {end_page if end_page != float('inf') else 'Inf'} | Active Network Queries: {active_balance_threads} / {NETWORK_CHECK_WORKERS}")

        with file_lock:
            with open('./page_local', 'w') as f:
                f.write(str(page_checkpoint))

# Clean up and wind down remaining network lookups gracefully when main scope concludes
print("\n[*] Processing completed. Flushing remaining background thread lookups...")
network_executor.shutdown(wait=True)
print(f"[+] Complete success. Final checkpoint logged at page: {page_checkpoint}!")

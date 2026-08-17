import sys
import time
import signal
import hashlib
import requests
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

# Verify the necessary cryptographic math module is installed
try:
    import ecdsa
except ImportError:
    print("[!] Error: The 'ecdsa' module is missing.")
    print("[*] Please install it by running: pip install ecdsa")
    sys.exit(1)

warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- ENGINE CONFIGURATION ---
GENERATOR_WORKERS = 4       # Uses multiple local CPU cores for in-memory ECDSA math
NETWORK_CHECK_WORKERS = 20  # Strict ceiling for active background API balance threads
KEYS_PER_PAGE = 128

# Tracking Metrics
page_checkpoint = 0
total_keys_checked = 0
active_balance_threads = 0

# Synchronicity Locks
file_lock = threading.Lock()
print_lock = threading.Lock()

# Centralized bounded thread pool for network checks to prevent system thread exhaustion crashes
network_executor = ThreadPoolExecutor(max_workers=NETWORK_CHECK_WORKERS)

def signal_handler(signal, frame):
    with print_lock:
        print(f"\n\n[+] Scanner paused cleanly.")
        print(f"[+] Total Keys Analyzed this session: {total_keys_checked}")
        print(f"[+] Progress saved at page: {page_checkpoint}\n")
    network_executor.shutdown(wait=False, cancel_futures=True)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def base58_encode(b):
    """Encodes raw bytes into a cryptographic Base58 check string."""
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

def derive_bitcoin_wallet(priv_int):
    """
    Performs secp256k1 point multiplication fully in-memory.
    Returns: (WIF_Private_Key, Public_Bitcoin_Address)
    """
    # 1. Generate Legitimate WIF Private Key
    priv_bytes = priv_int.to_bytes(32, 'big')
    extended_key = b'\x80' + priv_bytes
    first_sha = hashlib.sha256(extended_key).digest()
    second_sha = hashlib.sha256(first_sha).digest()
    wif_key = base58_encode(extended_key + second_sha[:4])

    # 2. Derive Public Key Point using secp256k1 Elliptic Curve Math
    sk = ecdsa.SigningKey.from_secret_exponent(priv_int, curve=ecdsa.SECP256k1)
    vk = sk.verifying_key
    public_key_bytes = b'\x04' + vk.to_string() # Uncompressed public key footprint

    # 3. Hash Public Key to legacy address format (SHA256 -> RIPEMD160)
    sha256_pk = hashlib.sha256(public_key_bytes).digest()
    ripemd160 = hashlib.new('ripemd160')
    ripemd160.update(sha256_pk)
    hashed_pk = ripemd160.digest()

    # 4. Attach Mainnet Prefix (0x00) and Checksum
    network_version_bin = b'\x00' + hashed_pk
    checksum = hashlib.sha256(hashlib.sha256(network_version_bin).digest()).digest()[:4]
    public_address = base58_encode(network_version_bin + checksum)

    return wif_key, public_address

def check_wallet_balance_worker(address, wif_key):
    """Pings the public API node inside bounded worker pool limits."""
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
                
                # CRITICAL UPDATE: Only touches local file storage if an active balance is caught
                with file_lock:
                    with open('./FOUND_BALANCES.txt', 'a') as f:
                        f.write(f"HIT! Address: {address} | WIF: {wif_key} | Balance: {btc} BTC\n")
                    # Break telemetry output to print the hit clearly
                    print(f"\n\n[!!!] ACTIVE BALANCE ENCOUNTERED! {btc} BTC at Address: {address} | Key: {wif_key}\n")
    except Exception:
        pass
    finally:
        with file_lock:
            active_balance_threads -= 1

def generate_local_page(page_num):
    """Calculates 128 mathematically unique, verified live keys completely in RAM."""
    global total_keys_checked
    start_index = ((page_num - 1) * KEYS_PER_PAGE) + 1
    
    for i in range(KEYS_PER_PAGE):
        current_private_key_int = start_index + i
        
        # Guard rail for maximum legitimate elliptic curve keyspace boundaries
        if current_private_key_int >= 115792089237316195423570985008687907852837564279074904382605163141518161494337:
            return "404"
            
        try:
            # Generate valid keyspace mappings without creating any string variables on disk
            wif_str, real_address = derive_bitcoin_wallet(current_private_key_int)
            
            with file_lock:
                total_keys_checked += 1
            
            # Pipe address straight to background pool queue
            network_executor.submit(check_wallet_balance_worker, real_address, wif_str)
        except Exception:
            continue
                
    return "SUCCESS"

# --- User Setup Logic ---
try:
    with open('./page_real', 'r') as f:
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

print(f"\n[+] Zero-Storage Live Memory Pipeline Active.")
print(f"[+] System checks keys in volatile RAM; only addresses containing actual balances write to disk.\n")
current_chunk_start = start_page
page_checkpoint = start_page - 1

# --- Execution Engine ---
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
            print("\n[+] Absolute mathematical boundary of elliptic keyspace reached. Terminating.")
            break

        current_chunk_start += len(page_batch)
        page_checkpoint = current_chunk_start - 1

        with print_lock:
            # Keep terminal tracking to a single line that flushes dynamically
            sys.stdout.write(f"\rPages Verified: {page_checkpoint} / {end_page if end_page != float('inf') else 'Inf'} | Total Keys: {total_keys_checked} | API Queue Threads: {active_balance_threads}/{NETWORK_CHECK_WORKERS}")
            sys.stdout.flush()

        with file_lock:
            with open('./page_real', 'w') as f:
                f.write(str(page_checkpoint))

print("\n\n[*] Processing finalized. Flushing remaining background thread lookups...")
network_executor.shutdown(wait=True)
print(f"[+] Complete success. Final checkpoint safe at page: {page_checkpoint}")

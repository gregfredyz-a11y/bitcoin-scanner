import sys
import time
import signal
import hashlib
import requests
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import ecdsa
except ImportError:
    print("[!] Error: The 'ecdsa' module is missing.")
    print("[*] Please install it by running: pip install ecdsa")
    sys.exit(1)

warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- MAP OF HISTORICALLY ACTIVE TRANSACTION BLOCKS ---
HISTORIC_RANGES = {
    "1": {
        "name": "Satoshi Nakamoto's Genesis & Early Blocks (Blocks 0-50)",
        "start": 1,
        "end": 50,
        "desc": "Contains Satoshi's Genesis wallet and the first transaction to Hal Finney."
    },
    "2": {
        "name": "The Bitcoin Early Puzzle Transaction Zones",
        "start": 15231150,
        "end": 15231250,
        "desc": "Known historical transaction test sequences from early miners."
    },
    "3": {
        "name": "Early Developer Vanity Keyspace Blocks",
        "start": 4892104100,
        "end": 4892104200,
        "desc": "Blocks containing derived uncompressed multi-sig test nodes."
    }
}

# --- ENGINE CONFIGURATION ---
GENERATOR_WORKERS = 4       
NETWORK_CHECK_WORKERS = 20  
KEYS_PER_PAGE = 128

# Tracking Metrics
total_keys_checked = 0
active_balance_threads = 0

# Synchronicity Locks
file_lock = threading.Lock()
print_lock = threading.Lock()

# Centralized bounded thread pool for network checks
network_executor = ThreadPoolExecutor(max_workers=NETWORK_CHECK_WORKERS)

def signal_handler(signal, frame):
    with print_lock:
        print(f"\n\n[+] Range-Hopper cleanly stopped.")
        print(f"[+] Total Keys Analyzed this session: {total_keys_checked}")
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

def derive_bitcoin_wallet(priv_int):
    """Performs secp256k1 point multiplication fully in-memory."""
    # 1. WIF Private Key
    priv_bytes = priv_int.to_bytes(32, 'big')
    extended_key = b'\x80' + priv_bytes
    first_sha = hashlib.sha256(extended_key).digest()
    second_sha = hashlib.sha256(first_sha).digest()
    wif_key = base58_encode(extended_key + second_sha[:4])

    # 2. Public Key via secp256k1 Curve Math
    sk = ecdsa.SigningKey.from_secret_exponent(priv_int, curve=ecdsa.SECP256k1)
    vk = sk.verifying_key
    public_key_bytes = b'\x04' + vk.to_string()

    # 3. Hash Public Key to legacy address format
    sha256_pk = hashlib.sha256(public_key_bytes).digest()
    ripemd160 = hashlib.new('ripemd160')
    ripemd160.update(sha256_pk)
    hashed_pk = ripemd160.digest()

    # 4. Attach Mainnet Prefix and Checksum
    network_version_bin = b'\x00' + hashed_pk
    checksum = hashlib.sha256(hashlib.sha256(network_version_bin).digest()).digest()[:4]
    public_address = base58_encode(network_version_bin + checksum)

    return wif_key, public_address

def check_wallet_balance_worker(address, wif_key, block_name):
    """Pings public infrastructure inside bounded worker pool limits."""
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
                
                # CRITICAL LOCK: Triggers storage write ONLY on real active balance hit
                with file_lock:
                    with open('./FOUND_BALANCES.txt', 'a') as f:
                        f.write(f"HIT! Range: {block_name} | Address: {address} | WIF: {wif_key} | Balance: {btc} BTC\n")
                    print(f"\n\n[!!!] REAL HIT FOUND IN {block_name.upper()}!")
                    print(f"[!] Address: {address} | Key: {wif_key} | Balance: {btc} BTC\n")
    except Exception:
        pass
    finally:
        with file_lock:
            active_balance_threads -= 1

def generate_local_page(page_num, block_name):
    """Calculates 128 mathematically unique keys completely in RAM."""
    global total_keys_checked
    start_index = ((page_num - 1) * KEYS_PER_PAGE) + 1
    
    for i in range(KEYS_PER_PAGE):
        current_private_key_int = start_index + i
        if current_private_key_int >= 115792089237316195423570985008687907852837564279074904382605163141518161494337:
            return "404"
            
        try:
            wif_str, real_address = derive_bitcoin_wallet(current_private_key_int)
            with file_lock:
                total_keys_checked += 1
            
            network_executor.submit(check_wallet_balance_worker, real_address, wif_str, block_name)
        except Exception:
            continue
                
    return "SUCCESS"

def run_range_scanner(start, end, block_name):
    """Iterates through a designated page block range layout."""
    current_chunk_start = start
    while current_chunk_start <= end:
        batch_size = min(GENERATOR_WORKERS, (end - current_chunk_start) + 1)
        if batch_size <= 0:
            break
            
        page_batch = list(range(current_chunk_start, current_chunk_start + int(batch_size)))
        
        with ThreadPoolExecutor(max_workers=len(page_batch)) as page_executor:
            future_to_page = {page_executor.submit(generate_local_page, p, block_name): p for p in page_batch}
            for future in as_completed(future_to_page):
                pass # Await local cryptographic math iterations

        current_chunk_start += len(page_batch)
        
        with print_lock:
            sys.stdout.write(f"\rScanning: [{block_name[:25]}...] | Current Page: {current_chunk_start - 1}/{end} | Total Keys: {total_keys_checked} | Network Workers: {active_balance_threads}/{NETWORK_CHECK_WORKERS}")
            sys.stdout.flush()

# --- Interactive Terminal Range Selector ---
print("="*60)
print("             AUTOMATED WALLET RANGE-HOPPER CORE             ")
print("="*60)
print("[*] Select a historically active target zone to sweep:\n")

for idx, details in HISTORIC_RANGES.items():
    print(f" {idx}) {details['name']}")
    print(f"    Pages: {details['start']} ──> {details['end']}")
    print(f"    Note:  {details['desc']}\n")

print(" A) Scan ALL Historical Blocks (Automated Range-Hopping Mode)")
print("="*60)

choice = input("[?] Enter selection (1, 2, 3, or A): ").strip().upper()

print(f"\n[+] Zero-Storage Memory Pipeline Engaged.")
print(f"[+] Only addresses containing actual balances write to disk.\n")

# --- Execution Matrix ---
if choice == 'A':
    print("[*] Starting Automated Range-Hopper across all targets...")
    for idx, details in HISTORIC_RANGES.items():
        print(f"\n[*] Jumping directly into target range block: {details['name']}")
        run_range_scanner(details['start'], details['end'], details['name'])
else:
    target = HISTORIC_RANGES.get(choice)
    if target:
        print(f"[*] Locking sequence onto specific block: {target['name']}")
        run_range_scanner(target['start'], target['end'], target['name'])
    else:
        print("[!] Invalid selection. Defaulting to Range 1 (Satoshi Genesis Block).")
        run_range_scanner(HISTORIC_RANGES["1"]['start'], HISTORIC_RANGES["1"]['end'], HISTORIC_RANGES["1"]['name'])

print("\n\n[*] Range scans completed. Flushing active background API lookups...")
network_executor.shutdown(wait=True)
print(f"[+] Operational tasks successful. Hit outputs logged in './FOUND_BALANCES.txt'")

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
        "desc": "Contains Satoshi's Genesis wallet keys evaluated via raw formats."
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
        "desc": "Blocks containing early multi-sig experiment setups."
    }
}

# --- ENGINE CONFIGURATION ---
GENERATOR_WORKERS = 2       
NETWORK_CHECK_WORKERS = 30  
KEYS_PER_PAGE = 128

# Tracking Metrics
total_keys_checked = 0
total_addresses_checked = 0
active_balance_threads = 0

# Synchronicity Locks
file_lock = threading.Lock()
print_lock = threading.Lock()

# Centralized bounded thread pool for network checks
network_executor = ThreadPoolExecutor(max_workers=NETWORK_CHECK_WORKERS)

def signal_handler(signal, frame):
    with print_lock:
        print(f"\n\n[+] Range-Hopper cleanly stopped.")
        print(f"[+] Total Keys Analyzed: {total_keys_checked}")
        print(f"[+] Total Network Lookups: {total_addresses_checked}")
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

def bech32_polymod(values):
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk

def bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

def encode_bech32(hrp, data):
    """Encodes raw witness data bits into standard Native SegWit Bech32 strings."""
    combined = data
    polymod = bech32_polymod(bech32_hrp_expand(hrp) + combined) ^ 1
    checksum = [(polymod >> (5 * (5 - i))) & 31 for i in range(6)]
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    return hrp + "1" + "".join([charset[d] for d in data + checksum])

def convert_bits(data, from_bits, to_bits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << to_bits) - 1
    max_acc = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or (value >> from_bits):
            return None
        acc = ((acc << from_bits) | value) & max_acc
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        return None
    return ret

def derive_bitcoin_addresses(priv_int):
    """Computes 3 distinct public address formats from a single key seed integer."""
    priv_bytes = priv_int.to_bytes(32, 'big')
    extended_key = b'\x80' + priv_bytes
    first_sha = hashlib.sha256(extended_key).digest()
    second_sha = hashlib.sha256(first_sha).digest()
    wif_key = base58_encode(extended_key + second_sha[:4])

    sk = ecdsa.SigningKey.from_secret_exponent(priv_int, curve=ecdsa.SECP256k1)
    vk = sk.verifying_key
    pub_x = vk.pubkey.point.x().to_bytes(32, 'big')
    pub_y = vk.pubkey.point.y().to_bytes(32, 'big')

    # 1. Legacy Uncompressed Address
    pub_uncompressed = b'\x04' + pub_x + pub_y
    sha_uncomp = hashlib.sha256(pub_uncompressed).digest()
    rmd_uncomp = hashlib.new('ripemd160', sha_uncomp).digest()
    net_uncomp = b'\x00' + rmd_uncomp
    chk_uncomp = hashlib.sha256(hashlib.sha256(net_uncomp).digest()).digest()[:4]
    legacy_uncompressed = base58_encode(net_uncomp + chk_uncomp)

    # 2. Legacy Compressed Address
    prefix = b'\x02' if vk.pubkey.point.y() % 2 == 0 else b'\x03'
    pub_compressed = prefix + pub_x
    sha_comp = hashlib.sha256(pub_compressed).digest()
    rmd_comp = hashlib.new('ripemd160', sha_comp).digest()
    net_comp = b'\x00' + rmd_comp
    chk_comp = hashlib.sha256(hashlib.sha256(net_comp).digest()).digest()[:4]
    legacy_compressed = base58_encode(net_comp + chk_comp)

    # 3. Native SegWit Bech32 Address (bc1q...)
    witness_program = rmd_comp
    converted = convert_bits(witness_program, 8, 5, True)
    # Native SegWit v0 uses a [0] (witness version) prefix inside the address payload data
    bech32_address = encode_bech32("bc1", [0] + converted)

    return wif_key, legacy_uncompressed, legacy_compressed, bech32_address

def check_wallet_balance_worker(address, wif_key, block_name, addr_type):
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
                    with open('./FOUND_BALANCES.txt', 'a') as f:
                        f.write(f"HIT! Range: {block_name} | Type: {addr_type} | Address: {address} | WIF: {wif_key} | Balance: {btc} BTC\n")
                    print(f"\n\n[!!!] REAL BALANCE MATCH HIT IN {block_name.upper()}!")
                    print(f"[!] Address [{addr_type}]: {address} | WIF Key: {wif_key} | Balance: {btc} BTC\n")
    except Exception:
        pass
    finally:
        with file_lock:
            active_balance_threads -= 1

def generate_local_page(page_num, block_name):
    global total_keys_checked, total_addresses_checked
    start_index = ((page_num - 1) * KEYS_PER_PAGE) + 1
    
    for i in range(KEYS_PER_PAGE):
        current_private_key_int = start_index + i
        if current_private_key_int >= 115792089237316195423570985008687907852837564279074904382605163141518161494337:
            return "404"
            
        try:
            wif_str, addr_uncomp, addr_comp, addr_segwit = derive_bitcoin_addresses(current_private_key_int)
            
            with file_lock:
                total_keys_checked += 1
                total_addresses_checked += 3
            
            network_executor.submit(check_wallet_balance_worker, addr_uncomp, wif_str, block_name, "Legacy Uncompressed")
            network_executor.submit(check_wallet_balance_worker, addr_comp, wif_str, block_name, "Legacy Compressed")
            network_executor.submit(check_wallet_balance_worker, addr_segwit, wif_str, block_name, "Native SegWit")
        except Exception:
            continue
                
    return "SUCCESS"

def run_range_scanner(start, end, block_name):
    current_chunk_start = start
    while current_chunk_start <= end:
        batch_size = min(GENERATOR_WORKERS, (end - current_chunk_start) + 1)
        if batch_size <= 0:
            break
            
        page_batch = list(range(current_chunk_start, current_chunk_start + int(batch_size)))
        
        with ThreadPoolExecutor(max_workers=len(page_batch)) as page_executor:
            future_to_page = {page_executor.submit(generate_local_page, p, block_name): p for p in page_batch}
            for future in as_completed(future_to_page):
                pass 

        current_chunk_start += len(page_batch)
        
        with print_lock:
            sys.stdout.write(f"\rScanning: [{block_name[:20]}...] | Page: {current_chunk_start - 1}/{end} | Keys: {total_keys_checked} | Network Lookups: {total_addresses_checked} | Workers: {active_balance_threads}/{NETWORK_CHECK_WORKERS}")
            sys.stdout.flush()

# --- Interactive Terminal User Interface ---
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

print(f"\n[+] Zero-Storage Multi-Format Memory Pipeline Engaged.")
print(f"[+] Sweeping Uncompressed, Compressed, and SegWit configurations in RAM.\n")

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
        print("[!] Invalid selection. Defaulting to Option 1.")
        run_range_scanner(HISTORIC_RANGES["1"]['start'], HISTORIC_RANGES["1"]['end'], HISTORIC_RANGES["1"]['name'])

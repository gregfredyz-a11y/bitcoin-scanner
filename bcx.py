import re
import sys
import time
import signal
import requests
import threading
from bs4 import BeautifulSoup
from urllib3.util import create_urllib3_context
from requests.adapters import HTTPAdapter

# --- TLS FIX FOR TERMUX ---
class TLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        # Downgrade strictness to handle older/broader ciphers on Python 3.14+
        context = create_urllib3_context()
        context.load_default_certs()
        context.set_ciphers('DEFAULT@SECLEVEL=1') 
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

# Initialize Session with the Hardened TLS Config
session = requests.Session()
session.mount('https://', TLSAdapter())
session.mount('http://', TLSAdapter())

# Spoof standard browser headers to bypass blockwalls
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5'
})

def signal_handler(signal, frame):
    global page
    sys.stdout.write("\nFinished on page: %d\n" % page)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

class WalletThread(threading.Thread):
    def __init__(self, url):
        self.url = url
        threading.Thread.__init__(self)

    def run(self):
        global processes
        processes += 1

        try:
            # Use the session instead of raw requests to carry headers/SSL fixes
            subreq = session.get(self.url, timeout=10)

            if subreq.status_code == 200:
                balance = BeautifulSoup(subreq.text, 'html.parser').find('td', id='final_balance')
                if balance:
                    value = float(balance.text.strip('BTC').strip())

                    if value > 0:
                        with open('./wallets_balance', 'a') as wallets_file:
                            wallets_file.write(
                                balance.text.strip() + '; ' + self.url + "\n"
                            )
        except Exception:
            pass # Silently drop connection failures in the checking thread
        finally:
            processes -= 1

# --- Initialization ---
page = 0
try:
    with open('./page', 'r') as f:
        page = int(f.read().strip())
except (IOError, ValueError):
    pass

url = 'http://directory.io/'  # Note: The adapter will catch any auto-redirect to https://
status = None
processes = 0

# Main loop
while status != 404:
    page += 1

    sys.stdout.write("\rPage: %d, processes runned: %d" % (page, processes))
    sys.stdout.flush()
    
    try:
        # Requesting the next batch via our custom TLS session
        req = session.post(f"{url}{page}", timeout=15)
        status = req.status_code

        if status != 404 and status == 200:
            soup = BeautifulSoup(req.text, 'html.parser')
            keys = soup.find('pre', {'class': 'keys'})

            if not keys:
                continue

            # Clear extra nodes
            for strong in keys.findAll('strong'):
                strong.decompose()

            # Process individual keys
            for wallet in str(keys).split("\n"):
                if not wallet.strip():
                    continue

                w_soup = BeautifulSoup(wallet, 'html.parser')

                for plus in w_soup.findAll('a', href=re.compile(r'warning')):
                    plus.decompose()

                wallet_key = None
                wallet_rsa = None
                wallet_url = None

                for block in w_soup:
                    if type(block).__name__ == 'NavigableString':
                        wallet_rsa = block.string.strip() if block.string else None

                    if type(block).__name__ == 'Tag':
                        wallet_url = block.get('href')
                        wallet_key = block.text

                # Log to localized flat files
                if wallet_key or wallet_rsa:
                    with open('./wallets', 'a') as wallets_file:
                        wallets_file.write(
                            f"{str(wallet_key)}; {str(wallet_rsa)}; {str(wallet_url)}\n"
                        )

                if wallet_url:
                    WalletThread(str(wallet_url)).start()

            # Record current progress checkpoint safely
            with open('./page', 'w') as f:
                f.write(str(page))
        
        elif status == 404:
            print("\nReached edge of database (404). Exiting.")
            break
        else:
            print(f"\n[!] Unexpected Server Response: {status}. Retrying...")
            page -= 1  # Re-try current page
            time.sleep(5)

    except requests.exceptions.RequestException as net_error:
        # Catch and seamlessly drop network drops without killing your total runtime progress
        print(f"\n[!] Network error on page {page}: {net_error}. Retrying in 5s...")
        page -= 1  # Rollback page counter to retry this index item
        time.sleep(5)

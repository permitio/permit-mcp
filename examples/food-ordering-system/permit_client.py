import os

import certifi

# CVE-2026-111943 (PER-17672): the permit SDK's aiohttp verifies against OpenSSL's default
# store, which can still trust eMudhra's compromised intermediate CAs (macOS's /etc/ssl/cert.pem
# does). Point it, and httpx, at certifi's bundle unless SSL_CERT_FILE is already set. aiohttp
# builds its SSL context when it is imported, so this stays above every other import.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from permit import Permit
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

PERMIT_PDP_URL = os.getenv("PERMIT_PDP_URL", 'https://cloudpdp.api.permit.io')
PERMIT_API_KEY = os.getenv("PERMIT_API_KEY")

permit = Permit(
    pdp=PERMIT_PDP_URL,
    token=PERMIT_API_KEY,
)

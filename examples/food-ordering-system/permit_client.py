import os
import ssl

import aiohttp.connector
import certifi
from permit import Permit
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


# CVE-2026-111943 (PER-17672): the permit SDK opens aiohttp sessions with no SSL context, so
# aiohttp verifies with its module default, built by ssl.create_default_context(). That loads
# the platform's trust store (OpenSSL's default file and directory, and on Windows the system
# stores too), which can still trust the compromised eMudhra intermediate CAs. Replace it with a
# context built the way httpx builds its own: SSL_CERT_FILE, else SSL_CERT_DIR, else certifi.
# aiohttp reads the default on every request, so it doesn't matter what imported aiohttp first.
def _verified_context() -> ssl.SSLContext:
    if os.environ.get("SSL_CERT_FILE"):
        context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    elif os.environ.get("SSL_CERT_DIR"):
        context = ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    context.set_alpn_protocols(("http/1.1",))  # as aiohttp sets on its own default
    return context


def _pin_aiohttp_trust_store() -> None:
    # A private name: fail loudly if an aiohttp upgrade drops it, rather than silently verify
    # against the platform's store again.
    if not hasattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED"):
        raise RuntimeError(
            "aiohttp.connector._SSL_CONTEXT_VERIFIED is gone, so the permit SDK's calls would "
            "verify against the platform trust store (CVE-2026-111943, PER-17672)"
        )
    aiohttp.connector._SSL_CONTEXT_VERIFIED = _verified_context()


# After load_dotenv(), so an SSL_CERT_FILE or SSL_CERT_DIR from .env applies here as it does to
# httpx, which reads them when each client is built.
_pin_aiohttp_trust_store()

PERMIT_PDP_URL = os.getenv("PERMIT_PDP_URL", 'https://cloudpdp.api.permit.io')
PERMIT_API_KEY = os.getenv("PERMIT_API_KEY")

permit = Permit(
    pdp=PERMIT_PDP_URL,
    token=PERMIT_API_KEY,
)

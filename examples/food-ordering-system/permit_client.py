import os
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

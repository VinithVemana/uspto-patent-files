"""
us/config.py — USPTO API constants and document-code classification sets
"""

import os
from dotenv import load_dotenv

load_dotenv()

API_KEY  = os.environ["USPTO_API_KEY"]
BASE_API = "https://api.uspto.gov/api/v1/patent/applications"
HEADERS  = {"X-API-KEY": API_KEY, "Accept": "application/json"}

# ---------------------------------------------------------------------------
# Document classification sets
# ---------------------------------------------------------------------------

OA_TRIGGER_CODES    = {"CTNF", "CTFR"}
OA_SUPPORTING_CODES = {"892", "FWCLM", "SRFW", "SRNT"}
OA_CODES            = OA_TRIGGER_CODES | OA_SUPPORTING_CODES

RESPONSE_CODES = {
    "REM", "CLM", "AMND",
    "A.1", "A.2", "A.3", "A...", "A.NE", "A.NE.AFCP",
    "AMSB", "RCEX", "RCE", "AFCP",
}

ADVISORY_CODES = {"CTAV"}
NOA_CODES      = {"NOA", "ISSUE.NOT"}

_RESP_DEFAULT_CODES = {"REM"}
_CLAIMS_CODES       = {"CLM"}

_OA_CODE_ORDER = {"CTNF": 0, "CTFR": 0, "892": 1, "FWCLM": 2, "SRFW": 3, "SRNT": 4}

# Fixed order for building the middle-bundle filename in 3-bundle mode
_MIDDLE_CODE_ORDER = ["REM", "CTNF", "NOA"]

# ---------------------------------------------------------------------------
# Continuation download settings  (edit these to change --continuations behavior)
# ---------------------------------------------------------------------------

# Parentage-type codes to follow when --continuations is used.
# CON = Continuation, CIP = Continuation-in-Part, DIV = Divisional
CONTINUATION_FOLLOW_CODES = {"CON", "CIP"}

# Which of the 3 prosecution bundles to download for each continuation parent.
#   "initial"  →  Initial_claims.pdf
#   "middle"   →  REM-CTNF-NOA.pdf
#   "granted"  →  Granted_claims.pdf
CONTINUATION_BUNDLES = ["middle"]

# ---------------------------------------------------------------------------
# Terminal Disclaimer download settings  (--disclaimers)
# ---------------------------------------------------------------------------

# Bundle types to download for each prior patent cited in an APPROVED
# Terminal Disclaimer (DISQ) decision. Same keys as CONTINUATION_BUNDLES.
DISCLAIMER_BUNDLES = ["middle"]

GOOGLE_PATENTS_HEADERS = {
    # A bare "Mozilla/5.0" UA is a known bot fingerprint and gets served a
    # 503 "We're sorry... automated queries" page. A full Chrome UA plus the
    # Accept* headers a real browser sends gets HTTP 200.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

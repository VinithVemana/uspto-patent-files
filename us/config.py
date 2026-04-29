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
# Source patent download settings  (the input/main patent in --download mode)
# ---------------------------------------------------------------------------

# Bundle types to download for the original source patent (the app number
# passed on the CLI). Files land in <root>/US{patent_no}/ (or app_{app_no}/)
# prefixed with US{patent_no}_ (or app_{app_no}_).
#   "initial"          →  {prefix}Initial_claims.pdf
#   "middle"           →  {prefix}REM-CTNF-NOA.pdf
#   "granted"          →  {prefix}Granted_claims.pdf
#   "index_of_claims"  →  {prefix}Index_of_claims.pdf  (most recent FWCLM)
#   "granted_document" →  {prefix}Granted_document.pdf  (full Google Patents PDF)
SOURCE_BUNDLES = ["initial", "middle", "granted", "index_of_claims", "granted_document"]

# ---------------------------------------------------------------------------
# Continuation download settings  (edit these to change --continuations behavior)
# ---------------------------------------------------------------------------

# Parentage-type codes to follow when --continuations is used.
# CON = Continuation, CIP = Continuation-in-Part, DIV = Divisional
CONTINUATION_FOLLOW_CODES = {"CON", "CIP"}

# Bundle types to download for each continuation parent. Files land in the
# same folder as the input patent's bundles, suffixed with _parent_{NN}:
#   "initial"          →  Initial_claims_parent_{NN}.pdf
#   "middle"           →  REM-CTNF-NOA_parent_{NN}.pdf
#   "granted"          →  Granted_claims_parent_{NN}.pdf
#   "index_of_claims"  →  Index_of_claims_parent_{NN}.pdf  (most recent FWCLM)
#   "granted_document" →  Granted_document_parent_{NN}.pdf  (full Google Patents PDF)
CONTINUATION_BUNDLES = ["initial", "middle", "granted", "index_of_claims", "granted_document"]

# ---------------------------------------------------------------------------
# Terminal Disclaimer download settings  (--disclaimers)
# ---------------------------------------------------------------------------

# Bundle types to download for each prior patent cited in an APPROVED
# Terminal Disclaimer (DISQ) decision. Same keys as CONTINUATION_BUNDLES.
# Files land in the same folder, suffixed with _TD_{NN}:
#   "initial"          →  Initial_claims_TD_{NN}.pdf
#   "middle"           →  REM-CTNF-NOA_TD_{NN}.pdf
#   "granted"          →  Granted_claims_TD_{NN}.pdf
#   "index_of_claims"  →  Index_of_claims_TD_{NN}.pdf  (most recent FWCLM)
#   "granted_document" →  Granted_document_TD_{NN}.pdf
DISCLAIMER_BUNDLES = ["initial", "middle", "granted", "index_of_claims", "granted_document"]

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

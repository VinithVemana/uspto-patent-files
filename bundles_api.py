"""
bundles_api.py — USPTO prosecution-bundle core library + CLI
=============================================================

INPUT FORMATS ACCEPTED
----------------------
All CLI arguments accept any of these formats:

  Application number     16123456          bare digits
  Formatted app number   16/123,456        slashes and commas stripped automatically
  Patent grant number    US10902286        'US' prefix → patent-to-app lookup
  Patent with kind code  US11973593B2      kind code (B2/B1/A1…) stripped automatically
  Bare patent digits     11973593          tries app number first, falls back to patent lookup
                         11973593 --patent  force patent-to-app lookup with --patent flag

  Resolution order when no 'US' prefix and no --patent flag:
    1. Try as application number (GET /meta-data)
    2. If not found, try as patent number (GET /search?q=applicationMetaData.patentNumber:…)


RUN FROM THE COMMAND LINE
-------------------------
    python bundles_api.py <number> [options]

    <number> — application number, patent grant number, or formatted variant (see above)

    Options:
      --patent            Force input to be treated as a patent grant number
                          (useful for bare digits like 11973593 that are ambiguous)
      --text              Human-readable text table (default output is JSON)
      --show-extra        Also include OA support docs, amendments, advisory actions, RCE docs
      --show-intclaim     Also include intermediate CLM docs in round bundles
      --download          Download each bundle as a merged PDF to disk;
                          also downloads the full granted patent PDF (patent.pdf) if available
      --output-dir DIR    Where to save PDFs (default: ./{app_no}/)
      --separate-bundles  One PDF per prosecution round (default: 3-bundle collapse)
      --base-url URL      Base URL for download_url links in JSON output
                          (default: http://localhost:7901)

    Examples — by application number:
      python bundles_api.py 16123456
      python bundles_api.py 16/123,456          # formatted — commas/slashes stripped
      python bundles_api.py 16123456 | jq .bundles[].download_url
      python bundles_api.py 16123456 --text
      python bundles_api.py 16123456 --show-extra --show-intclaim
      python bundles_api.py 16123456 --download --output-dir ./pdfs
      python bundles_api.py 16123456 --separate-bundles
      python bundles_api.py 16123456 --separate-bundles --download
      python bundles_api.py 16123456 --base-url https://myserver.example.com

    Examples — by patent grant number:
      python bundles_api.py US10902286           # US prefix → auto-resolve
      python bundles_api.py US10230476B1         # kind code stripped automatically
      python bundles_api.py 11973593 --patent    # bare digits, force patent route
      python bundles_api.py US10902286 --text
      python bundles_api.py US10902286 --download --output-dir ./pdfs


WEB SERVER
----------
    The FastAPI hosting layer lives in bundles_server.py.
    Run it with:
        uvicorn bundles_server:app --host 0.0.0.0 --port 7901
"""

import re
import io
import os
import time

import requests
from PyPDF2 import PdfWriter
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_KEY  = os.environ["USPTO_API_KEY"]
BASE_API = "https://api.uspto.gov/api/v1/patent/applications"
HEADERS  = {"X-API-KEY": API_KEY, "Accept": "application/json"}

# ---------------------------------------------------------------------------
# Document classification
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
_MIDDLE_CODE_ORDER = ["REM", "CTNF", "CTFR", "NOA"]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_json(url: str) -> dict | None:
    """GET with retry/backoff; returns parsed JSON or None."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2)
                continue
            return None
    return None


def resolve_patent_to_application(patent_digits: str) -> str | None:
    """
    Given a patent grant number as digits only (e.g. '10902286'),
    return the USPTO application number (e.g. '16123456').
    Returns None if not found.

    Caller must strip the 'US' prefix and kind codes (B2, B1, A1, etc.)
    before calling — use _extract_patent_digits() for that.
    """
    data = fetch_json(
        f"{BASE_API}/search"
        f"?q=applicationMetaData.patentNumber:{patent_digits}"
        f"&fields=applicationNumberText,applicationMetaData.patentNumber"
        f"&limit=1"
    )
    if not data:
        return None
    bag = data.get("patentFileWrapperDataBag", [])
    if not bag:
        return None
    return bag[0].get("applicationNumberText")


def resolve_publication_to_application(pub_number: str) -> str | None:
    """
    Given a pre-grant publication number in full form (e.g. 'US20210367709A1'),
    return the USPTO application number.
    Returns None if not found.

    The earliestPublicationNumber field stores the full string including 'US'
    prefix and kind code — stripping them produces a 404.
    """
    data = fetch_json(
        f"{BASE_API}/search"
        f"?q=applicationMetaData.earliestPublicationNumber:{pub_number}"
        f"&fields=applicationNumberText,applicationMetaData.earliestPublicationNumber"
        f"&limit=1"
    )
    if not data:
        return None
    bag = data.get("patentFileWrapperDataBag", [])
    if not bag:
        return None
    return bag[0].get("applicationNumberText")


def _is_publication_number(s: str) -> bool:
    """Return True if the string looks like a pre-grant publication (kind code A1/A2/A9)."""
    return bool(re.search(r"[Aa][129]\s*$", s.strip()))


def _extract_patent_digits(number: str) -> str:
    """
    Normalize a patent grant number string to digits only.

    Handles:
      - Formatting separators (commas, slashes, spaces): '11,973,593' → '11973593'
      - 'US' prefix: 'US10902286' → '10902286'
      - Kind codes at end: 'US11973593B2' → '11973593', '10902286B1' → '10902286'
    """
    s = re.sub(r"[,/\s]", "", number.strip())
    s = re.sub(r"(?i)^US", "", s)       # strip US prefix
    s = re.sub(r"[A-Za-z]\d*$", "", s)  # strip trailing kind code (B2, B1, A1 …)
    return re.sub(r"[^\d]", "", s)


def resolve_application_number(number: str, force_patent: bool = False) -> str:
    """
    Accept a USPTO application number or patent grant number in any common format.

    Input normalization (always applied first):
      - Commas, slashes, spaces stripped  →  '16/123,456' becomes '16123456'

    Resolution order:
      1. Input contains 'US' prefix with publication kind code (A1/A2/A9,
         e.g. 'US20210367709A1') → strip prefix + kind code, then do
         publication→app lookup via earliestPublicationNumber.
      2. Input contains 'US' prefix without publication kind code
         (e.g. 'US10902286', 'US11973593B2') → strip prefix + kind code,
         then do patent→app lookup via patentNumber.
      3. force_patent=True → treat bare digits as a patent number and do
         patent→app lookup directly.
      4. Bare digits only → try as an application number first; if USPTO
         returns no record, fall back to patent→app lookup.

    Raises ValueError when the input cannot be resolved.
    """
    s = re.sub(r"[,/\s]", "", number.strip())

    # Unambiguous: 'US' prefix present → publication or patent route
    if re.match(r"(?i)^US", s):
        if _is_publication_number(s):
            # Pass the full normalized string — earliestPublicationNumber stores
            # the complete value including 'US' prefix and kind code (e.g. 'US20210367709A1')
            app_no = resolve_publication_to_application(s)
        else:
            app_no = resolve_patent_to_application(_extract_patent_digits(s))
        if not app_no:
            raise ValueError(
                f"Could not resolve patent number '{number}' to a USPTO application number"
            )
        return app_no

    digits = re.sub(r"[^\d]", "", s)

    # Explicit flag → force patent route
    if force_patent:
        app_no = resolve_patent_to_application(digits)
        if not app_no:
            raise ValueError(
                f"Could not resolve '{number}' as a patent number to a USPTO application number"
            )
        return app_no

    # Ambiguous digits: try application number first, fall back to patent lookup
    if _get_metadata(digits):
        return digits
    app_no = resolve_patent_to_application(digits)
    if app_no:
        return app_no

    # Neither worked — return digits, caller will surface a clean 404
    return digits


def get_patent_pdf_url(patent_number: str) -> str | None:
    """
    Look up the full granted patent PDF URL from Google Patents.

    Fetches the Google Patents page for US{patent_number} and extracts the
    direct CDN link from patentimages.storage.googleapis.com.

    Returns the URL string, or None if the patent is not found / not yet
    available on Google Patents.
    """
    for kind_code in ["B2", "B1", ""]:
        gp_url = f"https://patents.google.com/patent/US{patent_number}{kind_code}/en"
        try:
            r = requests.get(
                gp_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            matches = re.findall(
                r"patentimages\.storage\.googleapis\.com/"
                r"([a-f0-9/]+/US" + re.escape(patent_number) + r"\.pdf)",
                r.text,
            )
            if matches:
                return f"https://patentimages.storage.googleapis.com/{matches[0]}"
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# USPTO data parsers
# ---------------------------------------------------------------------------

def _get_metadata(app_no: str) -> dict | None:
    data = fetch_json(f"{BASE_API}/{app_no}/meta-data")
    if not data or "patentFileWrapperDataBag" not in data:
        return None
    bag = data["patentFileWrapperDataBag"][0].get("applicationMetaData", {})

    inventors = []
    for inv in bag.get("inventorBag", []):
        loc = ""
        if "correspondenceAddressBag" in inv:
            a = inv["correspondenceAddressBag"][0]
            loc = f"{a.get('cityName', '')}, {a.get('countryName', '')}".strip(", ")
        inventors.append({"name": inv.get("inventorNameText", ""), "location": loc})

    return {
        "application_number": app_no,
        "title":         bag.get("inventionTitle", "N/A"),
        "status":        bag.get("applicationStatusDescriptionText", "N/A"),
        "filing_date":   bag.get("filingDate", ""),
        "examiner":      bag.get("examinerNameText", "Unassigned"),
        "art_unit":      bag.get("groupArtUnitNumber", "N/A"),
        "docket":        bag.get("docketNumber", "N/A"),
        "entity_status": bag.get("entityStatusData", {}).get("businessEntityStatusCategory", "N/A"),
        "app_type":      bag.get("applicationTypeLabelName", "Utility"),
        "patent_number": bag.get("patentNumber"),
        "grant_date":    bag.get("grantDate"),
        "pub_number":    bag.get("earliestPublicationNumber"),
        "pub_date":      bag.get("earliestPublicationDate"),
        "cpc_codes":     bag.get("cpcClassificationBag", []),
        "inventors":     inventors,
        "applicants":    [a.get("applicantNameText", "") for a in bag.get("applicantBag", [])],
    }


def _get_documents(app_no: str) -> list:
    data = fetch_json(f"{BASE_API}/{app_no}/documents")
    results = []
    if not data:
        return results

    for d in data.get("documentBag", []):
        doc_id = d.get("documentIdentifier", "")
        files, pdf_url = [], ""

        if d.get("downloadOptionBag"):
            for opt in d["downloadOptionBag"]:
                mime = opt.get("mimeTypeIdentifier", "UNK")
                if mime == "MS_WORD":
                    mime = "DOCX"
                url = opt.get("downloadUrl", "")
                files.append({"type": mime, "url": url})
                if mime == "PDF":
                    pdf_url = url

        if not files and doc_id:
            pdf_url = f"https://api.uspto.gov/api/v1/download/applications/{app_no}/{doc_id}.pdf"
            files.append({"type": "PDF", "url": pdf_url})

        pages = d.get("pageCount", 0)
        if not pages and d.get("downloadOptionBag"):
            pages = d["downloadOptionBag"][0].get("pageTotalQuantity", 0)

        results.append({
            "code":      d.get("documentCode", "UNK"),
            "desc":      d.get("documentCodeDescriptionText", "Unknown"),
            "date":      d.get("officialDate", ""),
            "direction": d.get("directionCategory", "INTERNAL"),
            "pages":     pages,
            "pdf_url":   pdf_url,
            "files":     files,
        })

    results.sort(key=lambda x: x["date"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Document classification helper
# ---------------------------------------------------------------------------

def _doc_category(code: str, bundle_type: str) -> str:
    """
    Return the visibility tier for a document code within a given bundle type.

    'default'  — always shown: CTNF, CTFR, NOA, REM, and CLM in initial/granted bundles
    'intclaim' — intermediate claims: CLM in round bundles (hidden by default)
    'extra'    — everything else: OA support, amendments, advisory, RCE (hidden by default)
    """
    if code in OA_TRIGGER_CODES or code in NOA_CODES or code in _RESP_DEFAULT_CODES:
        return "default"
    if code in _CLAIMS_CODES:
        return "default" if bundle_type in ("initial", "granted") else "intclaim"
    return "extra"


def _allowed_categories(show_extra: bool, show_intclaim: bool) -> set:
    cats = {"default"}
    if show_extra:    cats.add("extra")
    if show_intclaim: cats.add("intclaim")
    return cats


def _filter_docs(documents: list, bundle_type: str, show_extra: bool, show_intclaim: bool) -> list:
    """Return only the documents whose category is in the allowed set."""
    allowed = _allowed_categories(show_extra, show_intclaim)
    return [d for d in documents if _doc_category(d["code"], bundle_type) in allowed]


# ---------------------------------------------------------------------------
# Prosecution bundle builder
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.min


def _find_initial_claims(docs_asc: list) -> dict | None:
    """
    Return the operative initial-claims document (CLM).
    Priority:
      1. CLM within 7 days of a Preliminary Amendment (A.PE)
      2. Earliest INCOMING CLM
      3. Earliest CLM of any direction
    """
    ape_docs = [d for d in docs_asc if d["code"] == "A.PE"]
    if ape_docs:
        ape_date = _parse_date(ape_docs[0]["date"])
        nearby = [
            d for d in docs_asc
            if d["code"] == "CLM"
            and abs((_parse_date(d["date"]) - ape_date).days) <= 7
        ]
        if nearby:
            return min(nearby, key=lambda d: abs((_parse_date(d["date"]) - ape_date).days))

    clms = [d for d in docs_asc if d["code"] == "CLM"]
    if clms:
        incoming = [d for d in clms if d.get("direction", "").upper() == "INCOMING"]
        return (incoming or clms)[0]

    return None


def build_prosecution_bundles(app_no: str) -> list:
    """
    Return a list of bundle dicts (all documents included; filtering happens at display/download time).

    Bundle 0      — Operative initial claims
    Bundle 1..N   — Each OA round (OA trigger + support + applicant response)
    Bundle N+1    — Granted Claims (last CLM after first OA, only when NOA exists)
    """
    raw_docs = _get_documents(app_no)
    if not raw_docs:
        return []

    docs       = sorted(raw_docs, key=lambda d: d["date"])
    oa_anchors = [d for d in docs if d["code"] in OA_TRIGGER_CODES]
    noa_docs   = [d for d in docs if d["code"] in NOA_CODES]

    # --- Bundle 0: Initial Claims ---
    initial_clm = _find_initial_claims(docs)
    b0_docs = [initial_clm] if initial_clm else []
    if not oa_anchors and noa_docs:      # allowed without any rejection
        b0_docs.append(noa_docs[0])

    bundles: list = [{
        "index":     0,
        "label":     "Bundle 0 — Initial Claims",
        "type":      "initial",
        "documents": b0_docs,
    }]

    if not oa_anchors:
        return bundles

    # --- Bundles 1..N: One per Office Action ---
    for i, oa in enumerate(oa_anchors):
        is_last   = (i == len(oa_anchors) - 1)
        oa_date   = oa["date"]
        oa_day    = oa_date[:10]
        next_date = oa_anchors[i + 1]["date"] if not is_last else None

        oa_support = sorted(
            [d for d in docs if d["code"] in OA_SUPPORTING_CODES and d["date"][:10] == oa_day],
            key=lambda d: (_OA_CODE_ORDER.get(d["code"], 99), d["date"]),
        )
        window_docs = sorted(
            [
                d for d in docs
                if d["code"] in (RESPONSE_CODES | ADVISORY_CODES)
                and d["date"] > oa_date
                and (next_date is None or d["date"] < next_date)
            ],
            key=lambda d: d["date"],
        )

        round_docs = [oa] + oa_support + window_docs
        if is_last and noa_docs:
            round_docs.append(noa_docs[0])

        oa_type = "Final" if oa["code"] == "CTFR" else "Non-Final"
        label   = f"Bundle {i + 1} — Round {i + 1} ({oa_type})"
        if is_last and noa_docs:
            label += " + NOA"

        bundles.append({
            "index":     i + 1,
            "label":     label,
            "type":      "final_round" if is_last else "round",
            "documents": round_docs,
        })

    # --- Granted Claims Bundle ---
    first_oa_date = oa_anchors[0]["date"]
    response_clms = sorted(
        [d for d in docs if d["code"] == "CLM" and d["date"] > first_oa_date],
        key=lambda d: d["date"],
    )
    if response_clms and noa_docs:
        granted_idx = len(bundles)
        bundles.append({
            "index":     granted_idx,
            "label":     f"Bundle {granted_idx} — Granted Claims",
            "type":      "granted",
            "documents": [response_clms[-1]],
        })

    return bundles


def _build_three_bundles(bundles: list) -> list:
    """
    Collapse all prosecution rounds into exactly 3 logical groups:

      0 — initial_claims     (Bundle 0 docs)
      1 — {REM-CTNF-...}     (all round-bundle default-tier docs, sorted by date;
                               name built from whichever of REM/CTNF/CTFR/NOA are present)
      2 — granted_claims     (last granted bundle docs)

    Returns a list of dicts: {label, filename, type, documents}
    """
    initial = next((b for b in bundles if b["type"] == "initial"), None)
    granted = next((b for b in bundles if b["type"] == "granted"), None)
    rounds  = [b for b in bundles if b["type"] in ("round", "final_round")]

    # Middle: collect default-tier docs from every round, sorted by date
    middle_docs: list = []
    for b in rounds:
        middle_docs.extend(
            _filter_docs(b["documents"], b["type"], show_extra=False, show_intclaim=False)
        )
    middle_docs.sort(key=lambda d: d["date"])

    # Build filename from which key codes are actually present
    # ISSUE.NOT counts as NOA for naming purposes
    present = {d["code"] for d in middle_docs}
    if "ISSUE.NOT" in present:
        present.add("NOA")
    name_parts = [c for c in _MIDDLE_CODE_ORDER if c in present]
    middle_name = "-".join(name_parts) if name_parts else "prosecution"

    return [
        {
            "label":     "Initial Claims",
            "filename":  "initial_claims",
            "type":      "initial",
            "documents": initial["documents"] if initial else [],
        },
        {
            "label":     middle_name,
            "filename":  middle_name,
            "type":      "round",
            "documents": middle_docs,
        },
        {
            "label":     "Granted Claims",
            "filename":  "granted_claims",
            "type":      "granted",
            "documents": granted["documents"] if granted else [],
        },
    ]


# ---------------------------------------------------------------------------
# PDF merge helper (raises ValueError — converted to HTTPException by routes)
# ---------------------------------------------------------------------------

def _merge_bundle_pdfs(
    bundle: dict,
    show_extra: bool = False,
    show_intclaim: bool = False,
) -> io.BytesIO:
    """
    Fetch and merge PDFs for *bundle* filtered by the visibility flags.
    Raises ValueError when no PDFs are available or none could be fetched.
    """
    bundle_type = bundle.get("type", "round")
    visible = _filter_docs(bundle["documents"], bundle_type, show_extra, show_intclaim)
    pdf_docs = [d for d in visible if d.get("pdf_url")]

    if not pdf_docs:
        raise ValueError("No PDFs available in this bundle with the current flags")

    merger = PdfWriter()
    count  = 0
    for doc in pdf_docs:
        try:
            r = requests.get(doc["pdf_url"], headers=HEADERS, timeout=30)
            if r.status_code == 200:
                outline = f"{doc['code']} — {doc['desc']} ({doc['date'][:10]})"
                merger.append(io.BytesIO(r.content), outline_item=outline)
                count += 1
        except Exception as e:
            print(f"PDF fetch failed [{doc.get('pdf_url')}]: {e}")

    if count == 0:
        raise ValueError("Could not retrieve any valid PDFs for this bundle")

    out = io.BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)
    return out


# ---------------------------------------------------------------------------
# Index-of-Claims PDF helper
# ---------------------------------------------------------------------------

def _merge_fwclm_pdf(bundles: list) -> io.BytesIO:
    """
    Collect all FWCLM (Index of Claims) docs across all prosecution bundles,
    merge their PDFs in date order, and return the merged BytesIO.
    Raises ValueError when no FWCLM docs are found or none could be fetched.
    """
    seen, fwclm_docs = set(), []
    for b in bundles:
        for doc in b["documents"]:
            if doc["code"] == "FWCLM" and doc.get("pdf_url") and doc["pdf_url"] not in seen:
                seen.add(doc["pdf_url"])
                fwclm_docs.append(doc)
    fwclm_docs.sort(key=lambda d: d["date"])

    if not fwclm_docs:
        raise ValueError("No FWCLM (Index of Claims) documents found")

    merger = PdfWriter()
    count  = 0
    for doc in fwclm_docs:
        try:
            r = requests.get(doc["pdf_url"], headers=HEADERS, timeout=30)
            if r.status_code == 200:
                merger.append(
                    io.BytesIO(r.content),
                    outline_item=f"FWCLM — {doc['desc']} ({doc['date'][:10]})",
                )
                count += 1
        except Exception as e:
            print(f"PDF fetch failed [{doc.get('pdf_url')}]: {e}")

    if count == 0:
        raise ValueError("Could not retrieve any FWCLM PDFs")

    out = io.BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)
    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import os
    import sys

    parser = argparse.ArgumentParser(
        description="Fetch prosecution bundles for a USPTO application (JSON output by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 3-bundle mode (default): initial_claims + REM-CTNF-... + granted_claims
  python bundles_api.py 16123456
  python bundles_api.py 16123456 --download --output-dir ./pdfs

  # Human-readable text table
  python bundles_api.py 16123456 --text

  # One PDF per prosecution round (original per-round mode)
  python bundles_api.py 16123456 --separate-bundles
  python bundles_api.py 16123456 --separate-bundles --show-extra --show-intclaim
  python bundles_api.py 16123456 --separate-bundles --download --output-dir ./pdfs

  # Custom base URL for download_url links in separate-bundles mode
  python bundles_api.py 16123456 --separate-bundles --base-url https://myserver.example.com
        """,
    )
    parser.add_argument("application_number",
                        help="USPTO application number (e.g. 16123456) or patent grant number "
                             "(e.g. US10902286, US11973593B2, 11973593). "
                             "Formatting like '16/123,456' is accepted.")
    parser.add_argument("--separate-bundles", action="store_true",
                        help="One PDF per prosecution round (default: merge into 3 PDFs)")
    parser.add_argument("--show-extra",       action="store_true",
                        help="(--separate-bundles only) Include OA support docs, amendments, advisory, RCE docs")
    parser.add_argument("--show-intclaim",    action="store_true",
                        help="(--separate-bundles only) Include intermediate CLM docs in round bundles")
    parser.add_argument("--download",         action="store_true",
                        help="Download each bundle as a merged PDF to disk")
    parser.add_argument("--output-dir",       default=None,
                        help="Directory to save PDFs (default: ./{app_no}/)")
    parser.add_argument("--base-url",         default="http://localhost:7901",
                        help="(--separate-bundles only) Base URL for download_url links "
                             "(default: http://localhost:7901)")
    parser.add_argument("--patent",            action="store_true",
                        help="Force input to be treated as a patent grant number "
                             "(useful when passing bare digits like 11973593 that could be "
                             "either an application or patent number)")
    parser.add_argument("--text",             action="store_true",
                        help="Print a human-readable text table instead of JSON")
    args = parser.parse_args()

    # --- Resolve & fetch ---
    print(f"Resolving {args.application_number} ...", file=sys.stderr)
    try:
        app_no = resolve_application_number(args.application_number, force_patent=args.patent)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Application number: {app_no}", file=sys.stderr)

    meta = _get_metadata(app_no)
    if not meta:
        print(f"ERROR: Application '{args.application_number}' not found in USPTO.", file=sys.stderr)
        sys.exit(1)

    bundles = build_prosecution_bundles(app_no)
    if not bundles:
        print("No prosecution documents found.", file=sys.stderr)
        sys.exit(0)

    output_dir = args.output_dir if args.output_dir is not None else app_no

    def _download_patent_pdf() -> None:
        """Download the full granted patent PDF (patent.pdf) if the app has been granted."""
        patent_no = meta.get("patent_number")
        if not patent_no:
            print("  (no patent number — application not yet granted, skipping patent.pdf)",
                  file=sys.stderr)
            return
        filename = f"US{patent_no}.pdf"
        filepath = os.path.join(output_dir, filename)
        print(f"  Fetching full patent PDF for US{patent_no} ...", file=sys.stderr)
        pdf_url = get_patent_pdf_url(patent_no)
        if not pdf_url:
            print(f"  Patent PDF not found on Google Patents for US{patent_no}", file=sys.stderr)
            return
        try:
            r = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60, stream=True)
            r.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    fh.write(chunk)
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  Saved {filename} ({size_kb:,} KB)  <-  {pdf_url}", file=sys.stderr)
        except Exception as exc:
            print(f"  Failed to download patent PDF: {exc}", file=sys.stderr)

    def _download_index_of_claims() -> None:
        """Download all FWCLM docs merged into Index_of_claims.pdf."""
        filepath = os.path.join(output_dir, "Index_of_claims.pdf")
        print("  Fetching Index of Claims (FWCLM) ...", file=sys.stderr)
        try:
            pdf = _merge_fwclm_pdf(bundles)
            with open(filepath, "wb") as fh:
                fh.write(pdf.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  Saved Index_of_claims.pdf ({size_kb:,} KB)", file=sys.stderr)
        except ValueError as exc:
            print(f"  Index of Claims not available: {exc}", file=sys.stderr)

    # ================================================================== SEPARATE-BUNDLES mode
    if args.separate_bundles:
        base         = args.base_url.rstrip("/")
        flag_qs      = f"?show_extra={str(args.show_extra).lower()}&show_intclaim={str(args.show_intclaim).lower()}"
        total_rounds = sum(1 for b in bundles if b["type"] in ("round", "final_round"))

        result_bundles = []
        for bundle in bundles:
            bundle_type  = bundle["type"]
            visible_docs = _filter_docs(bundle["documents"], bundle_type, args.show_extra, args.show_intclaim)
            result_bundles.append({
                "index":        bundle["index"],
                "label":        bundle["label"],
                "type":         bundle_type,
                "download_url": f"{base}/bundles/{app_no}/{bundle['index']}/pdf{flag_qs}",
                "documents":    visible_docs,
            })

        if not args.text:
            output = {**meta, "total_rounds": total_rounds, "bundles": result_bundles}
            print(json.dumps(output, indent=2))
            if args.download:
                os.makedirs(output_dir, exist_ok=True)
                for bundle, rb in zip(bundles, result_bundles):
                    if not rb["documents"]:
                        continue
                    safe     = re.sub(r"[^\w\s\-]", "", bundle["label"]).strip().replace(" ", "_")
                    filepath = os.path.join(output_dir, f"{safe}.pdf")
                    print(f"Downloading bundle {rb['index']} -> {filepath} ...", file=sys.stderr)
                    try:
                        pdf = _merge_bundle_pdfs(bundle, args.show_extra, args.show_intclaim)
                        with open(filepath, "wb") as fh:
                            fh.write(pdf.getvalue())
                        size_kb = os.path.getsize(filepath) // 1024
                        print(f"  Saved ({size_kb:,} KB)", file=sys.stderr)
                    except ValueError as exc:
                        print(f"  Failed: {exc}", file=sys.stderr)
                _download_patent_pdf()
                _download_index_of_claims()
            sys.exit(0)

        # Text output
        print("=" * 64)
        print(f"Title:         {meta['title']}")
        print(f"Status:        {meta['status']}")
        print(f"Filing date:   {meta['filing_date']}")
        print(f"Patent no.:    {meta.get('patent_number') or 'N/A'}")
        print(f"Grant date:    {meta.get('grant_date') or 'N/A'}")
        print(f"Pub no.:       {meta.get('pub_number') or 'N/A'}")
        print(f"Examiner:      {meta['examiner']}  (AU {meta['art_unit']})")
        print(f"Inventors:     {', '.join(i['name'] for i in meta['inventors']) or 'N/A'}")
        print(f"Applicants:    {', '.join(meta['applicants']) or 'N/A'}")
        print("=" * 64)
        print(f"\nBundles: {len(result_bundles)}   OA rounds: {total_rounds}\n")

        if args.download:
            os.makedirs(output_dir, exist_ok=True)

        for bundle, rb in zip(bundles, result_bundles):
            bundle_type = bundle["type"]
            print(f"[{rb['index']}] {rb['label']}")
            print(f"    Download: {rb['download_url']}")
            if not rb["documents"]:
                print("    (no documents visible with current flags)")
            else:
                for doc in rb["documents"]:
                    pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                    cat   = _doc_category(doc["code"], bundle_type)
                    tag   = {"default": "", "intclaim": " [int-claim]", "extra": " [extra]"}.get(cat, "")
                    print(f"    {doc['date'][:10]}  {doc['code']:<12}  "
                          f"{doc['desc'][:48]:<48}  {pages:>4}{tag}")
            if args.download and rb["documents"]:
                safe     = re.sub(r"[^\w\s\-]", "", bundle["label"]).strip().replace(" ", "_")
                filepath = os.path.join(output_dir, f"{safe}.pdf")
                print(f"    -> Downloading to {filepath} ...")
                try:
                    pdf = _merge_bundle_pdfs(bundle, args.show_extra, args.show_intclaim)
                    with open(filepath, "wb") as fh:
                        fh.write(pdf.getvalue())
                    size_kb = os.path.getsize(filepath) // 1024
                    print(f"    -> Saved ({size_kb:,} KB)")
                except ValueError as exc:
                    print(f"    -> Failed: {exc}")
            print()
        if args.download:
            _download_patent_pdf()
            _download_index_of_claims()
        sys.exit(0)

    # ================================================================== DEFAULT: 3-bundle mode
    three = _build_three_bundles(bundles)

    def _download_three(b: dict) -> None:
        """Merge and save one of the 3 logical bundles to disk."""
        filepath = os.path.join(output_dir, f"{b['filename']}.pdf")
        print(f"    -> Downloading to {filepath} ...")
        try:
            pdf = _merge_bundle_pdfs({"type": b["type"], "documents": b["documents"]},
                                     show_extra=False, show_intclaim=False)
            with open(filepath, "wb") as fh:
                fh.write(pdf.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"    -> Saved ({size_kb:,} KB)")
        except ValueError as exc:
            print(f"    -> Failed: {exc}")

    if not args.text:
        output = {
            **meta,
            "bundles": [
                {"filename": b["filename"], "label": b["label"],
                 "type": b["type"], "documents": b["documents"]}
                for b in three
            ],
        }
        print(json.dumps(output, indent=2))
        if args.download:
            os.makedirs(output_dir, exist_ok=True)
            for b in three:
                if b["documents"]:
                    _download_three(b)
            _download_patent_pdf()
            _download_index_of_claims()
        sys.exit(0)

    # Text output
    print("=" * 64)
    print(f"Title:         {meta['title']}")
    print(f"Status:        {meta['status']}")
    print(f"Filing date:   {meta['filing_date']}")
    print(f"Patent no.:    {meta.get('patent_number') or 'N/A'}")
    print(f"Grant date:    {meta.get('grant_date') or 'N/A'}")
    print(f"Pub no.:       {meta.get('pub_number') or 'N/A'}")
    print(f"Examiner:      {meta['examiner']}  (AU {meta['art_unit']})")
    print(f"Inventors:     {', '.join(i['name'] for i in meta['inventors']) or 'N/A'}")
    print(f"Applicants:    {', '.join(meta['applicants']) or 'N/A'}")
    print("=" * 64)
    print(f"\n3-bundle mode  (use --separate-bundles for one PDF per round)\n")

    if args.download:
        os.makedirs(output_dir, exist_ok=True)

    for b in three:
        print(f"[{b['filename']}]")
        if not b["documents"]:
            print("    (no documents)")
        else:
            for doc in b["documents"]:
                pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                print(f"    {doc['date'][:10]}  {doc['code']:<12}  "
                      f"{doc['desc'][:48]:<48}  {pages:>4}")
        if args.download and b["documents"]:
            _download_three(b)
        print()

    if args.download:
        _download_patent_pdf()
        _download_index_of_claims()

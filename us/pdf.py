"""
us/pdf.py — USPTO PDF fetch helpers (patent PDF + bundle merging)
"""

import io
import re
import sys
import time

import requests
from PyPDF2 import PdfWriter

from .config import HEADERS, GOOGLE_PATENTS_HEADERS
from .bundles import _filter_docs


def get_patent_pdf_url(patent_number: str) -> str | None:
    """
    Look up the full granted patent PDF URL from Google Patents.

    Retries each kind code up to 3 times with exponential backoff, and
    logs the actual failure reason (status code or exception) to stderr
    so transient bot-block 503s don't look identical to a real 404.
    """
    pdf_regex = (
        r"patentimages\.storage\.googleapis\.com/"
        r"([a-f0-9/]+/US" + re.escape(patent_number) + r"\.pdf)"
    )
    for kind_code in ["B2", "B1", ""]:
        gp_url = f"https://patents.google.com/patent/US{patent_number}{kind_code}/en"
        for attempt in range(3):
            try:
                r = requests.get(gp_url, headers=GOOGLE_PATENTS_HEADERS, timeout=15)
                if r.status_code == 404:
                    break
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    print(
                        f"    Google Patents {r.status_code} for "
                        f"US{patent_number}{kind_code} (attempt {attempt + 1}/3)",
                        file=sys.stderr,
                    )
                    if attempt < 2:
                        time.sleep((attempt + 1) * 2)
                        continue
                    break
                if r.status_code != 200:
                    break
                matches = re.findall(pdf_regex, r.text)
                if matches:
                    return f"https://patentimages.storage.googleapis.com/{matches[0]}"
                break
            except requests.RequestException as exc:
                print(
                    f"    Google Patents error for US{patent_number}{kind_code} "
                    f"(attempt {attempt + 1}/3): {exc}",
                    file=sys.stderr,
                )
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
    return None


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

    # Use only the most recent FWCLM document
    fwclm_docs = [fwclm_docs[-1]]

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

"""
us/disclaimer.py — Terminal Disclaimer (DISQ) parsing

DISQ docs are scanned PTOL forms (image-only PDFs). We need OCR to extract:
  - approval status (APPROVED / DISAPPROVED)
  - list of prior US patent numbers the disclaimer covers

Pipeline: download PDF -> pdftoppm to PNGs -> tesseract OCR -> regex parse.
Both pdftoppm and tesseract must be on PATH (system binaries).
"""

import os
import re
import subprocess
import sys
import tempfile

import requests

from .config import HEADERS
from .client import _get_documents


DISQ_CODE = "DISQ"


def _ocr_pdf_url(url: str) -> str:
    """Download a PDF, OCR every page, return concatenated text. Empty on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
    except Exception as exc:
        print(f"  [DISQ OCR] download failed for {url}: {exc}", file=sys.stderr)
        return ""

    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, "in.pdf")
        with open(pdf_path, "wb") as fh:
            fh.write(r.content)

        try:
            subprocess.run(
                ["pdftoppm", "-r", "300", pdf_path, os.path.join(td, "p"), "-png"],
                check=True, capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print(f"  [DISQ OCR] pdftoppm failed: {exc}", file=sys.stderr)
            return ""

        pages = sorted(f for f in os.listdir(td) if f.endswith(".png"))
        chunks = []
        for png in pages:
            png_path = os.path.join(td, png)
            try:
                out = subprocess.run(
                    ["tesseract", png_path, "-"],
                    check=True, capture_output=True, text=True,
                )
                chunks.append(out.stdout)
            except (FileNotFoundError, subprocess.CalledProcessError) as exc:
                print(f"  [DISQ OCR] tesseract failed on {png}: {exc}", file=sys.stderr)
        return "\n".join(chunks)


_PATENT_RE = re.compile(r"\b(\d{1,2},\d{3},\d{3})\b")
_PATENT_DIGITS_RE = re.compile(r"\b(\d{7,8})\b")


def _normalize_patent_no(s: str) -> str:
    return re.sub(r"[^\d]", "", s)


def parse_disq_text(text: str) -> dict:
    """
    Parse OCR text from a DISQ form.

    Returns:
        {
          "approved": bool | None,   # None = couldn't determine
          "patents":  list[str],     # patent numbers as digit-only strings
        }
    """
    low = text.lower()

    approved: bool | None = None
    if "tds approved" in low or "td approved" in low:
        approved = True
    elif "tds disapproved" in low or "td disapproved" in low:
        approved = False
    else:
        # fallback: checkbox-style "[x] approved"
        if re.search(r"(?:\[x\]|x\])\s*approved", low):
            approved = True
        elif re.search(r"(?:\[x\]|x\])\s*disapproved", low):
            approved = False
        elif "approved" in low and "disapproved" not in low:
            approved = True

    patents: list[str] = []
    for m in _PATENT_RE.finditer(text):
        digits = _normalize_patent_no(m.group(1))
        if digits and digits not in patents:
            patents.append(digits)

    # Some DISQ forms list patents without commas; only fall back to bare-digit
    # capture when no comma form was found, to avoid grabbing app numbers etc.
    if not patents:
        for m in _PATENT_DIGITS_RE.finditer(text):
            digits = m.group(1)
            # skip the instant application number range (12/14-digit unlikely here)
            if 7 <= len(digits) <= 8 and digits not in patents:
                patents.append(digits)

    return {"approved": approved, "patents": patents}


def get_disq_decisions(app_no: str) -> list[dict]:
    """
    For app_no, find every DISQ doc, OCR it, parse decision + cited patents.

    Returns a list (most recent first) of:
        {
          "date":      str,           # ISO date from /documents
          "pdf_url":   str,
          "approved":  bool | None,
          "patents":   list[str],     # digit-only patent numbers
        }
    """
    docs = _get_documents(app_no)
    out = []
    for d in docs:
        if d["code"] != DISQ_CODE:
            continue
        url = d.get("pdf_url") or ""
        if not url:
            continue
        print(f"  [DISQ] OCR {d['date'][:10]}  {url}", file=sys.stderr)
        text = _ocr_pdf_url(url)
        parsed = parse_disq_text(text)
        out.append({
            "date":     d["date"],
            "pdf_url":  url,
            "approved": parsed["approved"],
            "patents":  parsed["patents"],
        })
    return out

"""
ep/register_client.py — EPO Register (register.epo.org) scraper
===============================================================

The EPO OPS API does NOT expose prosecution document PDFs. The authoritative
source for communications, office actions, replies and grant decisions is the
public EPO Register website at register.epo.org.

The register is behind Cloudflare + a JSP session. Two-step access:

  1. Warm the session by hitting a document-list page. This seeds JSESSIONID
     and the Cloudflare cookies (__cf_bm, _cfuvid).
  2. Fetch each PDF via the iframe-style URL
        https://register.epo.org/application?showPdfPage=1
             &documentId={docId}&appnumber=EP{appNum}&proc=
     using the same Session object so the cookies travel along.

Exposed API:
  - RegisterSession.warm(app_number) — establishes cookies
  - RegisterSession.list_documents(app_number) -> list[dict]
  - RegisterSession.fetch_pdf(doc_id, app_number) -> bytes  (raw PDF bytes)

Each returned document dict:
  {
    "doc_id":    "EQT180184244FI4",
    "date":      "2010-10-05",          # ISO format
    "doc_type":  "European search report",
    "procedure": "Search / examination",
    "pages":     2,
  }
"""

from __future__ import annotations

import io
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfWriter

REGISTER_BASE = "https://register.epo.org"

# Full Chrome UA + Accept headers — a bare "Mozilla/5.0" fails Cloudflare.
_BROWSER_HEADERS = {
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
}


class RegisterSession:
    """
    Thin wrapper over requests.Session that manages cookie warmup and provides
    doclist + PDF access. Thread-unsafe by design — create one per concurrent
    request if needed.
    """

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers.update(_BROWSER_HEADERS)
        self._warmed_for: str | None = None

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, timeout: int = 20) -> requests.Response:
        """
        GET a path on register.epo.org with retry logic:
          - 429 / 5xx: transient server errors — retry after 2s, 4s
          - 403:       Cloudflare rate-limit (transient) — retry after 20s, 60s
          - network errors: retry after 2s, 4s
        """
        url = f"{REGISTER_BASE}{path}"
        for attempt in range(3):
            try:
                r = self._s.get(url, timeout=timeout)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    if attempt < 2:
                        time.sleep((attempt + 1) * 2)
                        continue
                elif r.status_code == 403:
                    if attempt < 2:
                        # Cloudflare transient rate-limit — wait longer before retry
                        wait = 20 if attempt == 0 else 60
                        time.sleep(wait)
                        continue
                return r
            except requests.RequestException:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                raise
        return r  # last response (will have non-200 status)

    # ------------------------------------------------------------------
    # Session warmup — hit the doclist page to seed cookies
    # ------------------------------------------------------------------

    def warm(self, app_number: str) -> None:
        """
        Establish session cookies by GETting the doclist for *app_number*.
        Subsequent PDF requests for the same session will succeed.

        app_number: EP application number WITH 'EP' prefix (e.g. 'EP10173239').
                    Digits-only accepted; 'EP' is prepended automatically.
        """
        app_num = app_number if app_number.upper().startswith("EP") else f"EP{app_number}"
        if self._warmed_for == app_num and self._s.cookies:
            return
        r = self._get(f"/application?number={app_num}&tab=doclist")
        if r.status_code != 200:
            raise RuntimeError(
                f"Failed to warm register session for {app_num} "
                f"[HTTP {r.status_code}]"
            )
        self._warmed_for = app_num

    # ------------------------------------------------------------------
    # Document list — scrape the doclist HTML table
    # ------------------------------------------------------------------

    def list_documents(self, app_number: str) -> list[dict]:
        """
        Fetch and parse the document list for an EP application.
        Returns a list of dicts, sorted by date (oldest first).

        Returns empty list if no documents are available (e.g. the application
        has just been filed and no register entries exist yet).
        """
        app_num = app_number if app_number.upper().startswith("EP") else f"EP{app_number}"
        r = self._get(f"/application?number={app_num}&tab=doclist")
        if r.status_code != 200:
            raise RuntimeError(
                f"Doclist fetch failed for {app_num} [HTTP {r.status_code}]"
            )
        self._warmed_for = app_num

        docs: list[dict] = []
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.find_all("tr"):
            cb = row.find("input", {"name": "identivier"})
            if not cb:
                continue
            doc_id = cb.get("value", "").strip()
            if not doc_id:
                continue
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            date_raw  = cols[1].get_text(strip=True)
            doc_type  = cols[2].get_text(strip=True)
            procedure = cols[3].get_text(strip=True).replace("\xa0", " ")
            pages_raw = cols[4].get_text(strip=True)

            docs.append({
                "doc_id":    doc_id,
                "date":      _to_iso_date(date_raw),
                "doc_type":  doc_type,
                "procedure": procedure,
                "pages":     _parse_pages(pages_raw),
            })

        docs.sort(key=lambda d: d["date"])
        if not docs and "identivier" not in r.text:
            # Cloudflare returned a challenge/block page instead of the real doclist
            raise RuntimeError(
                f"Doclist for {app_num} returned 0 documents and no document "
                f"table — likely a Cloudflare challenge page. "
                f"First 200 chars: {r.text[:200]!r}"
            )
        return docs

    # ------------------------------------------------------------------
    # PDF fetch — the iframe-style showPdfPage URL returns application/pdf
    # ------------------------------------------------------------------

    def fetch_pdf(self, doc_id: str, app_number: str, *, pages: int = 1, timeout: int = 45) -> bytes:
        """
        Download all pages of the PDF for *doc_id* and return merged bytes.

        pages: total page count from the doclist (default 1 for callers that
               don't have it). Each EPO Register page is a separate HTTP fetch.

        Raises RuntimeError if any page is not a valid PDF.
        """
        app_num = app_number if app_number.upper().startswith("EP") else f"EP{app_number}"
        if self._warmed_for != app_num:
            self.warm(app_num)

        page_count = max(pages, 1)
        if page_count == 1:
            return self._fetch_page(doc_id, app_num, 1, timeout)

        merger = PdfWriter()
        for page_num in range(1, page_count + 1):
            if page_num > 1:
                time.sleep(0.3)
            # Per-page retry: Cloudflare may block mid-document; wait and re-warm
            for attempt in range(3):
                try:
                    page_bytes = self._fetch_page(doc_id, app_num, page_num, timeout)
                    break
                except RuntimeError:
                    if attempt < 2:
                        wait = 30 if attempt == 0 else 90
                        self._warmed_for = None
                        self.warm(app_num)
                        time.sleep(wait)
                    else:
                        raise
            merger.append(io.BytesIO(page_bytes))
        out = io.BytesIO()
        merger.write(out)
        merger.close()
        out.seek(0)
        return out.read()

    def _fetch_page(self, doc_id: str, app_num: str, page_num: int, timeout: int) -> bytes:
        """Fetch a single page of a register document. Re-warms session on non-PDF response."""
        path = (
            f"/application?showPdfPage={page_num}"
            f"&documentId={doc_id}"
            f"&appnumber={app_num}"
            f"&proc="
        )
        r = self._get(path, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(
                f"PDF fetch HTTP {r.status_code} for doc_id={doc_id} "
                f"page={page_num} ({app_num})"
            )
        content = r.content
        if not content.startswith(b"%PDF"):
            self._warmed_for = None
            self.warm(app_num)
            r = self._get(path, timeout=timeout)
            content = r.content
            if not content.startswith(b"%PDF"):
                raise RuntimeError(
                    f"Non-PDF response for doc_id={doc_id} page={page_num} "
                    f"(content-type={r.headers.get('Content-Type','?')}, "
                    f"first 16 bytes={content[:16]!r})"
                )
        return content


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _to_iso_date(raw: str) -> str:
    """Convert 'DD.MM.YYYY' → 'YYYY-MM-DD'. Returns '' if unparseable."""
    raw = raw.strip()
    if not raw:
        return ""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # unknown format — preserve for debugging


def _parse_pages(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else 0

"""
ep/register_client.py — EPO Register (register.epo.org) scraper
===============================================================

The EPO OPS API does NOT expose prosecution document PDFs. The authoritative
source for communications, office actions, replies and grant decisions is the
public EPO Register website at register.epo.org.

The register is behind Cloudflare, but only headless Chromium triggers the
JS-challenge.  Plain requests with a Firefox UA bypasses CF entirely: the
server returns the real doclist HTML and sets JSESSIONID, which is then
sufficient for POST /application.

PDF download uses a three-level fallback chain, fastest to slowest:

  Path A — POST /application (whole doc, 1 request)
    POST /application with doc_id + JSESSIONID → EPO assembles the whole
    document server-side and returns one PDF.  No cf_clearance needed.
    Requires JSESSIONID (established by the GET warm).  On 403 the session
    is re-warmed (fresh GET) and the POST is retried once.

  Path B — Smart page fetch (_fetch_pages_smart)
    Phase 1 (parallel, 2 workers, fast-fail): all pages attempted concurrently
    via direct session.get() — no 20/60s CF-retry waits. Successfully fetched
    pages are cached. Pages that 403 or return non-PDF are collected.
    Phase 2 (sequential retry): only the failed pages are retried with full
    CF re-warm logic (30/90s backoff). Successfully cached pages are reused,
    never re-fetched. Net result: faster for docs where CF allows concurrent
    requests; gracefully degrades to sequential for CF-blocked pages.

  Path C — KOPD re-probe (in pdf.merge_bundle_pdfs, bundle level)
    When ALL EPO Register fetches for a bundle fail, reset KOPD's reachability
    cache and try again. If KOPD is now up, match EPO Register docs by
    date + doc_type and download the same content via KOPD.
    Handled in ep/pdf.py, not here.

NOTE on Playwright: tested and confirmed to be UNRELIABLE for EPO Register.
  - Headless Chromium (even with stealth patches) always hits CF "Just a
    moment" challenge and never resolves it.
  - Headless Firefox bypasses CF, but non-deterministically (sometimes works,
    sometimes blocked depending on CF's IP-based token bucket state).
  - Plain requests + Firefox UA is the only reliable bypass: it bypasses
    CF every time (with retry on transient 403s) and sets JSESSIONID.
Playwright code is removed entirely — no Playwright import, no warm thread.

Exposed API:
  - RegisterSession.warm(app_number) — establishes JSESSIONID via GET
  - RegisterSession.list_documents(app_number) -> list[dict]
  - RegisterSession.fetch_pdf(doc_id, app_number) -> bytes

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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfWriter

REGISTER_BASE = "https://register.epo.org"

# Firefox UA bypasses Cloudflare on EPO Register.
# Chrome/Chromium UAs (including stealth-patched headless Chromium) always
# trigger the CF JS-challenge and never pass it.  Firefox UA with plain
# requests works because CF's rule is Chromium-automation-specific, not
# UA-string-specific — a bare requests client with Firefox UA has no
# navigator.webdriver, no CDP signals, and no headless-GPU fingerprint.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) "
        "Gecko/20100101 Firefox/119.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


class RegisterSession:
    """
    Thin wrapper over requests.Session that manages cookie warmup and provides
    doclist + PDF access.

    Cloudflare bypass strategy: Firefox UA with plain requests.Session.
    CF's Managed Challenge fires specifically on headless Chromium automation
    signals (navigator.webdriver, CDP fingerprint, headless GPU flags).
    Firefox UA with requests bypasses this completely — the server sees a
    non-browser client with a Gecko UA and applies no JS challenge.

    On the first GET, EPO Register sets JSESSIONID + __cf_bm + _cfuvid.
    JSESSIONID is sufficient for POST /application to return PDF bytes.
    No cf_clearance needed or possible (it is a Chromium-JS-challenge token).

    fetch_pdf() uses a three-level fallback chain (see module docstring).
    _warm_lock (RLock) serialises concurrent re-warm calls from parallel workers.
    """

    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers.update(_BROWSER_HEADERS)
        self._warmed_for: str | None = None
        self._warm_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, timeout: int = 20) -> requests.Response:
        """
        GET a path on register.epo.org with retry logic:
          - 429 / 5xx: transient server errors — retry after 2s, 4s
          - 403:       CF transient rate-limit — fresh session + retry 3s, 6s
          - network errors: retry after 2s, 4s

        On 403, a new requests.Session is created (fresh cookie jar) because
        CF's token-bucket appears to be keyed partly on cookie state — reusing
        the same poisoned session does not help.
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
                        wait = (3, 10, 30)[attempt]
                        print(
                            f"  [register] GET 403 (CF rate-limit), "
                            f"fresh session + {wait}s wait (attempt {attempt+1}/3)",
                            file=sys.stderr,
                        )
                        time.sleep(wait)
                        # New session clears any CF-poisoned cookie state
                        self._s = requests.Session()
                        self._s.headers.update(_BROWSER_HEADERS)
                        continue
                return r
            except requests.RequestException:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                raise
        return r  # last response (will have non-200 status)

    # ------------------------------------------------------------------
    # Session warmup
    # ------------------------------------------------------------------

    def warm(self, app_number: str) -> None:
        """
        Establish JSESSIONID for *app_number* via a single GET to the doclist.

        Firefox UA bypasses Cloudflare reliably.  The GET sets JSESSIONID,
        __cf_bm and _cfuvid — JSESSIONID is all that's needed for POST /application.

        Already-warmed sessions return immediately (no-op).
        Transient 403s are retried automatically by _get() with a fresh session.
        """
        app_num = app_number if app_number.upper().startswith("EP") else f"EP{app_number}"

        if self._warmed_for == app_num and self._s.cookies.get("JSESSIONID"):
            return  # already have a valid session

        r = self._get(f"/application?number={app_num}&tab=doclist")
        if r.status_code != 200:
            raise RuntimeError(
                f"Failed to warm register session for {app_num} "
                f"[HTTP {r.status_code}]"
            )
        self._warmed_for = app_num
        jsid = self._s.cookies.get("JSESSIONID", "")
        print(
            f"  [register] session warmed for {app_num} — "
            f"JSESSIONID={'yes' if jsid else 'NO'}",
            file=sys.stderr,
        )

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
            raise RuntimeError(
                f"Doclist for {app_num} returned 0 documents and no document "
                f"table — likely a Cloudflare challenge page. "
                f"First 200 chars: {r.text[:200]!r}"
            )
        return docs

    # ------------------------------------------------------------------
    # PDF fetch — four-level fallback chain
    # ------------------------------------------------------------------

    def fetch_pdf(self, doc_id: str, app_number: str, *, pages: int = 1, timeout: int = 45) -> bytes:
        """
        Download all pages of *doc_id* and return merged PDF bytes.

        Tries paths in order: POST whole-doc → parallel GETs → sequential GETs.
        (KOPD re-probe is handled one level up in pdf.merge_bundle_pdfs.)

        pages: total page count from the doclist (default 1).
        """
        app_num = app_number if app_number.upper().startswith("EP") else f"EP{app_number}"
        if self._warmed_for != app_num:
            self.warm(app_num)

        page_count = max(pages, 1)

        # Path A: POST /application — whole doc in one request.
        # JSESSIONID (set by warm()) is sufficient — no cf_clearance required.
        # On 403 the session is re-warmed and retried once before falling back.
        try:
            pdf = self._post_fetch_pdf(doc_id, app_num, timeout=timeout + 15)
            print(f"  [register] path=POST doc={doc_id}", file=sys.stderr)
            return pdf
        except Exception as exc:
            print(
                f"  [register] POST failed ({exc}) — trying parallel GET",
                file=sys.stderr,
            )

        # Paths B+D: smart fetch — parallel fast-attempt, sequential retry for failures
        return self._fetch_pages_smart(doc_id, app_num, page_count, timeout)

    def reset(self) -> None:
        """Force a new requests.Session (fresh cookie jar) and clear warmed state."""
        self._s = requests.Session()
        self._s.headers.update(_BROWSER_HEADERS)
        self._warmed_for = None

    def _post_fetch_pdf(self, doc_id: str, app_num: str, timeout: int = 60) -> bytes:
        """
        POST /application with a single doc_id — EPO assembles and returns the
        whole document as one PDF.  Requires JSESSIONID (set by warm()).

        On 403: fresh session + re-warm + retry. Three attempts with 3s/10s/30s
        waits between them. Raises RuntimeError if still failing after all retries.
        """
        _403_waits = (3, 10, 30)
        for attempt in range(3):
            r = self._s.post(
                f"{REGISTER_BASE}/application",
                data={"documentIdentifiers": doc_id, "number": app_num},
                headers={
                    "Referer": f"{REGISTER_BASE}/application?number={app_num}&tab=doclist",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": REGISTER_BASE,
                },
                timeout=timeout,
            )
            if r.status_code == 403:
                if attempt < 2:
                    wait = _403_waits[attempt]
                    print(
                        f"  [register] POST 403 for {doc_id} — re-warming session "
                        f"+ {wait}s wait (attempt {attempt+1}/3)",
                        file=sys.stderr,
                    )
                    self._warmed_for = None
                    self._s = requests.Session()
                    self._s.headers.update(_BROWSER_HEADERS)
                    time.sleep(wait)
                    self.warm(app_num)
                    continue
                raise RuntimeError(
                    f"POST /application 403 (CF blocked) for {doc_id} after 3 attempts"
                )
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise RuntimeError(
                    f"POST /application non-PDF for {doc_id}: "
                    f"status={r.status_code}, CT={r.headers.get('content-type','?')}, "
                    f"first 16={r.content[:16]!r}"
                )
            return r.content
        raise RuntimeError(f"POST /application exhausted retries for {doc_id}")

    def _fetch_pages_smart(
        self, doc_id: str, app_num: str, page_count: int, timeout: int
    ) -> bytes:
        """
        Two-phase page fetch:

        Phase 1 — parallel (2 workers, fast-fail, no CF-retry waits):
          All pages attempted concurrently. Uses direct self._s.get() so a
          403 or non-PDF raises immediately without the 20/60s retry sleeps
          in self._get(). Successfully fetched pages are cached.

        Phase 2 — sequential retry (only for pages that failed in phase 1):
          Each failed page is retried with self._fetch_page() (which does the
          CF re-warm on non-PDF) and the full 3-attempt + 30/90s backoff loop.

        This avoids two problems with a pure parallel approach:
          * CF rate-limit waits (20s each) accumulating in parallel threads.
          * Re-fetching pages that already succeeded when falling back to
            sequential after a partial failure.
        """
        if page_count == 1:
            return self._fetch_page(doc_id, app_num, 1, timeout)

        page_bytes: dict[int, bytes] = {}
        failed_pages: list[int] = []

        def _fetch_one_fast(n: int) -> tuple[int, bytes]:
            if n % 2 == 0:
                time.sleep(0.15)  # stagger the two workers
            path = (
                f"/application?showPdfPage={n}"
                f"&documentId={doc_id}"
                f"&appnumber={app_num}"
                f"&proc="
            )
            r = self._s.get(f"{REGISTER_BASE}{path}", timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if not r.content.startswith(b"%PDF"):
                raise RuntimeError("non-PDF response")
            return n, r.content

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {ex.submit(_fetch_one_fast, n): n for n in range(1, page_count + 1)}
            for fut in as_completed(futures):
                n = futures[fut]
                try:
                    _, data = fut.result()
                    page_bytes[n] = data
                except Exception:
                    failed_pages.append(n)

        if failed_pages:
            print(
                f"  [register] {len(failed_pages)}/{page_count} pages failed parallel "
                f"— retrying sequentially (doc={doc_id})",
                file=sys.stderr,
            )
            for page_num in sorted(failed_pages):
                time.sleep(0.1)
                _page_waits = (30, 90, 180)
                for attempt in range(4):
                    try:
                        page_bytes[page_num] = self._fetch_page(
                            doc_id, app_num, page_num, timeout
                        )
                        break
                    except RuntimeError:
                        if attempt < 3:
                            wait = _page_waits[attempt]
                            print(
                                f"  [register] page {page_num} failed, re-warm "
                                f"+ {wait}s wait (attempt {attempt+1}/4)",
                                file=sys.stderr,
                            )
                            with self._warm_lock:
                                self._warmed_for = None
                                self._s = requests.Session()
                                self._s.headers.update(_BROWSER_HEADERS)
                                self.warm(app_num)
                            time.sleep(wait)
                        else:
                            raise

        pages_fetched = len(page_bytes)
        path_label = "parallel" if not failed_pages else f"parallel+sequential({len(failed_pages)} retried)"
        print(
            f"  [register] path={path_label} pages={pages_fetched}/{page_count} doc={doc_id}",
            file=sys.stderr,
        )

        merger = PdfWriter()
        for i in range(1, page_count + 1):
            merger.append(io.BytesIO(page_bytes[i]))
        out = io.BytesIO()
        merger.write(out)
        merger.close()
        out.seek(0)
        return out.read()

    def _fetch_page(self, doc_id: str, app_num: str, page_num: int, timeout: int) -> bytes:
        """
        Fetch a single page of a register document.
        Re-warms session once (thread-safe via _warm_lock) on non-PDF response.
        """
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
            with self._warm_lock:
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

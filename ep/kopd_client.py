"""
ep/kopd_client.py — KOPD (KIPO Open Patent Database) doc fetcher
================================================================

Cloudflare-free alternative to register.epo.org for EP prosecution docs.
KOPD (`kopd.kipo.go.kr:8888`) proxies the same EPO doc inventory via SOAP
to KIPO's public-facing site. We use it as the *primary* source for EP
prosecution docs and fall back to `RegisterSession` (the EPO Register
scraper) when KOPD is unreachable, IP-blocked, or its SOAP backend
returns an error.

Why KOPD is useful here:
  * No Cloudflare. Sidesteps the JS-challenge / Turnstile mitigation
    blocking the EPO Register from our IP.
  * Server-rendered HTML + a single JSON AJAX endpoint — no Selenium
    needed despite KIPO's own reference scripts using it.

Caveats:
  * KOPD's EP path proxies to EPO SOAP, so wide EPO outages take both
    sources down. KOPD specifically sidesteps Cloudflare on the
    front-end.
  * TLS 1.2 strict — server refuses newer handshakes (connection reset).
    We pin TLSv1.2 via a custom `HTTPAdapter`.
  * Aggressive per-IP rate limiting — repeated requests trigger TCP
    resets. Conservative retry/backoff is built in.

Endpoint summary
----------------
- ``POST /kipi/getDocList2.do``
  Form body: ``docdbNum=EP.{app_no}.A``
  Returns JSON: ``{"result": "success", "doclist": [...]}``
  Each doclist entry: ``{docid, docid2, rs_doc_nm, rs_dt, numberOfPage,
                          docformat, docgroup_en, acss_cp_rst_tpcd, ...}``

- ``POST /docContent/download.do``
  Form body: ``docdbnum=EP.{app_no}.A`` + one ``check`` field per doc,
  value = ``{docid}!@#{numberOfPage}!@#{docformat}!@#{rs_dt}!@#{rs_doc_nm}``.
  Response: ``application/zip`` containing the PDFs.

Public API
----------
- ``is_reachable()`` — cached TCP probe.
- ``list_documents(app_no) -> list[dict]`` — normalised to the same
  shape ``RegisterSession.list_documents`` returns (with `_kopd` raw
  fields stashed for the downloader).
- ``fetch_doc_pdf(doc) -> bytes`` — single-doc download; unzips and
  returns the first PDF inside.
- ``merge_bundle_pdfs(bundle, ...) -> io.BytesIO`` — convenience that
  fetches every doc in a bundle and merges them via PyPDF2, mirroring
  ``ep/pdf.merge_bundle_pdfs``.
"""

from __future__ import annotations

import io
import socket
import ssl
import sys
import time
import zipfile
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from PyPDF2 import PdfWriter
from requests.adapters import HTTPAdapter


KOPD_BASE_URL = "https://kopd.kipo.go.kr:8888"
KOPD_HOST     = "kopd.kipo.go.kr"
KOPD_PORT     = 8888

# KOPD encodes form-value separators with this magic string in the download form.
_CHECK_SEP = "!@#"

# Browser-flavoured headers — KOPD doesn't bot-mitigate but a real UA is polite.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

HTTP_TIMEOUT = 30
TCP_TIMEOUT  = 3.0

# Soft-failure markers in KOPD's "result" field (KOPD proxies to EPO SOAP and
# surfaces SOAP/backend errors verbatim). We treat these as cache misses so
# the caller can fall back to EPO Register.
_SOFT_FAILURE_MARKERS = (
    "PrivilegedActionException",
    "SOAPException",
    "System is not available",
    "Application number provided was not found",
    "not found",
)


# ---------------------------------------------------------------------------
# TLS-1.2 transport adapter
# ---------------------------------------------------------------------------

class _Tls12Adapter(HTTPAdapter):
    """Pins TLS 1.2 on the underlying SSL context — KOPD rejects newer."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ---------------------------------------------------------------------------
# Shared session (lazy)
# ---------------------------------------------------------------------------

_session: requests.Session | None = None


def _session_() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.mount("https://", _Tls12Adapter())
        s.headers.update(_BROWSER_HEADERS)
        _session = s
    return _session


# ---------------------------------------------------------------------------
# Reachability probe (cached per process, mirrors pcs_api / srch11)
# ---------------------------------------------------------------------------

_reachable_cache: bool | None = None


def is_reachable() -> bool:
    """TCP probe to KOPD, cached for the process. Conservative — 3s timeout."""
    global _reachable_cache
    if _reachable_cache is not None:
        return _reachable_cache
    try:
        with socket.create_connection((KOPD_HOST, KOPD_PORT), timeout=TCP_TIMEOUT):
            _reachable_cache = True
            print(f"  [kopd] TCP probe to {KOPD_HOST}:{KOPD_PORT} succeeded — "
                  f"will use KOPD for EP doclist", file=sys.stderr)
    except (OSError, socket.timeout) as exc:
        _reachable_cache = False
        print(f"  [kopd] TCP probe to {KOPD_HOST}:{KOPD_PORT} failed ({exc}) — "
              f"will fall back to EPO Register", file=sys.stderr)
    return _reachable_cache


def _reset_reachable_cache() -> None:
    """Test helper — reset reachability cache so the next call re-probes."""
    global _reachable_cache
    _reachable_cache = None


# ---------------------------------------------------------------------------
# Low-level HTTP with retry
# ---------------------------------------------------------------------------

def _post_with_retry(path: str, data: dict, *, timeout: int = HTTP_TIMEOUT,
                     attempts: int = 3, want_json: bool = False) -> requests.Response:
    """POST with exponential backoff on TCP-reset / 5xx (KOPD rate-limits hard)."""
    url = f"{KOPD_BASE_URL}{path}"
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            r = _session_().post(url, data=data, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if i < attempts - 1:
                    wait = 2 ** (i + 1)
                    print(f"  [kopd] HTTP {r.status_code} from {path} — "
                          f"backing off {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
            return r
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if i < attempts - 1:
                wait = 2 ** (i + 1)
                print(f"  [kopd] network error on {path} ({exc}) — "
                      f"backing off {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return r  # pragma: no cover — for type-checker


# ---------------------------------------------------------------------------
# Doc listing
# ---------------------------------------------------------------------------

def _build_docdb(app_no: str) -> str:
    """Construct KOPD's docdb identifier from an EP application number."""
    digits = "".join(c for c in app_no if c.isdigit())
    return f"EP.{digits}.A"


def list_documents(app_no: str) -> list[dict]:
    """
    Fetch the prosecution doc list from KOPD for an EP application.

    Returns a list of dicts normalised to the same shape as
    ``RegisterSession.list_documents``:

      {"doc_id": ..., "date": "YYYY-MM-DD", "doc_type": ..., "procedure":
       ..., "pages": int, "_kopd": {...raw fields needed by download...}}

    Restricted-access docs (``acss_cp_rst_tpcd != ""``) are filtered out.

    Raises:
      ValueError      — on missing app_no.
      RuntimeError    — on soft failures from KOPD's SOAP relay
                        (caller should treat as cache miss and fall back).
      requests.exceptions.RequestException — on hard network failures.
    """
    if not app_no:
        raise ValueError("list_documents requires app_no")

    docdb = _build_docdb(app_no)
    print(f"  [kopd] querying doclist for {docdb} ...", file=sys.stderr)

    r = _post_with_retry(
        "/kipi/getDocList2.do",
        data={"docdbNum": docdb},
    )
    r.raise_for_status()

    try:
        payload: dict[str, Any] = r.json()
    except ValueError as exc:
        raise RuntimeError(f"KOPD non-JSON response: {exc} | first 200: {r.text[:200]!r}")

    result = str(payload.get("result", ""))
    if result != "success":
        if any(m in result for m in _SOFT_FAILURE_MARKERS):
            raise RuntimeError(f"KOPD soft failure: {result[:200]}")
        raise RuntimeError(f"KOPD unexpected result: {result[:200]}")

    raw_list = payload.get("doclist") or []
    docs: list[dict] = []
    skipped_restricted = 0

    for d in raw_list:
        if not d.get("docid") or d["docid"] == "-":
            continue
        if d.get("acss_cp_rst_tpcd"):
            skipped_restricted += 1
            continue
        pages_raw = d.get("numberOfPage") or "1"
        try:
            pages = int("".join(c for c in str(pages_raw) if c.isdigit()) or 1)
        except ValueError:
            pages = 1
        docs.append({
            "doc_id":    d["docid"],
            "date":      _to_iso_date(d.get("rs_dt", "")),
            "doc_type":  d.get("rs_doc_nm", ""),
            "procedure": d.get("docgroup_en", ""),
            "pages":     pages,
            "_kopd": {
                "docdb":         docdb,
                "docid":         d["docid"],
                "docformat":     d.get("docformat", ""),
                "rs_dt":         d.get("rs_dt", ""),
                "rs_doc_nm":     d.get("rs_doc_nm", ""),
                "numberOfPage":  str(pages_raw),
            },
        })

    docs.sort(key=lambda x: x["date"])
    print(f"  [kopd] {len(docs)} docs returned"
          + (f" (skipped {skipped_restricted} restricted)" if skipped_restricted else ""),
          file=sys.stderr)
    return docs


# ---------------------------------------------------------------------------
# Doc download
# ---------------------------------------------------------------------------

def fetch_doc_pdf(doc: dict, *, timeout: int = 60) -> bytes:
    """
    Download a single doc's PDF from KOPD. Returns raw PDF bytes.

    ``doc`` must carry a ``_kopd`` sub-dict from ``list_documents`` (the
    download endpoint needs the raw check-value fields). KOPD returns a
    ZIP wrapping one PDF; we extract and return that PDF's bytes.
    """
    raw = doc.get("_kopd")
    if not raw:
        raise ValueError("fetch_doc_pdf requires a doc dict with `_kopd` raw fields")

    check_val = _CHECK_SEP.join([
        raw["docid"],
        raw.get("numberOfPage", "1"),
        raw.get("docformat", ""),
        raw.get("rs_dt", ""),
        raw.get("rs_doc_nm", ""),
    ])
    data = {"docdbnum": raw["docdb"], "check": check_val}

    print(f"  [kopd] downloading {raw['docid']} ({doc.get('doc_type','?')[:40]})",
          file=sys.stderr)
    r = _post_with_retry("/docContent/download.do", data=data, timeout=timeout)
    r.raise_for_status()

    if r.content.startswith(b"%PDF"):
        return r.content  # raw PDF (some endpoints stream it directly)
    return _extract_first_pdf_from_zip(r.content)


def _extract_first_pdf_from_zip(content: bytes) -> bytes:
    """Pull the first PDF entry from a ZIP payload."""
    if not content.startswith(b"PK"):
        raise RuntimeError(
            f"KOPD download not a ZIP or PDF (first 16 bytes={content[:16]!r})"
        )
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".pdf"):
                with zf.open(name) as fh:
                    return fh.read()
    raise RuntimeError("KOPD ZIP contained no PDF entries")


# ---------------------------------------------------------------------------
# Bundle merge — mirrors ep.pdf.merge_bundle_pdfs for callsite parity
# ---------------------------------------------------------------------------

def merge_bundle_pdfs(bundle: dict, *, progress_cb=None) -> io.BytesIO:
    """
    Fetch every doc in ``bundle['documents']`` from KOPD and merge into one PDF.

    Raises ValueError when the bundle is empty or all fetches failed.
    """
    docs = bundle.get("documents") or []
    if not docs:
        raise ValueError("No documents in this bundle")

    merger = PdfWriter()
    count    = 0
    failures: list[tuple[str, str]] = []
    for doc in docs:
        if progress_cb is not None:
            progress_cb(doc)
        try:
            pdf_bytes = fetch_doc_pdf(doc)
            outline = f"[{doc.get('procedure','?')[:8]}] {doc.get('doc_type','?')} ({doc.get('date','')})"
            merger.append(io.BytesIO(pdf_bytes), outline_item=outline)
            count += 1
        except Exception as exc:
            failures.append((doc.get("doc_id", "?"), str(exc)))
        time.sleep(0.3)  # be polite to KOPD rate limiter

    if count == 0:
        detail = "; ".join(f"{did}: {err[:80]}" for did, err in failures[:3])
        raise ValueError(f"KOPD: no PDFs retrieved — {detail or 'no docs had doc_id'}")

    out = io.BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_iso_date(raw: str) -> str:
    """Normalise KOPD's `rs_dt` (varies: YYYY-MM-DD, YYYYMMDD, DD.MM.YYYY)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # unknown format — preserve for debugging

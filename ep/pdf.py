"""
ep/pdf.py — Session-aware PDF merger for EP register documents
==============================================================

Fetches each document's PDF via a shared RegisterSession (cookie warmup once,
reuse for every download in the bundle) and merges them into one output PDF
with bookmarks labelled "[CODE] — doc_type (YYYY-MM-DD)".

When ALL EPO Register fetches fail for a bundle, a KOPD re-probe is attempted
(path C in the four-level chain): reset KOPD's reachability cache, and if KOPD
is now reachable, fetch a fresh KOPD doclist and match docs by date + doc_type.

Exposed API:
  - merge_bundle_pdfs(session, bundle, app_number, *, show_extra, show_intclaim)
  - doc_fingerprint(docs) — for manifest-based change detection
"""

from __future__ import annotations

import hashlib
import io
import sys
import time

from PyPDF2 import PdfWriter

from . import bundles as _bundles
from . import kopd_client as _kopd
from .register_client import RegisterSession


def merge_bundle_pdfs(
    session: RegisterSession,
    bundle: dict,
    app_number: str,
    *,
    show_extra: bool = False,
    show_intclaim: bool = False,
    progress_cb=None,
) -> io.BytesIO:
    """
    Fetch and merge PDFs for a single bundle. Returns a BytesIO positioned at 0.

    bundle: a dict with "documents" (list) and "type" keys (see ep.bundles).
    app_number: EP application number (digits only or 'EP' prefix — both ok).
    progress_cb: optional callable(doc) invoked before each PDF download,
                 used to feed a tqdm bar with current-item detail.

    Raises ValueError when no PDFs are available or all fetches failed.
    """
    visible = _bundles._filter_docs(
        bundle["documents"], show_extra=show_extra, show_intclaim=show_intclaim
    )
    if not visible:
        raise ValueError("No documents in this bundle with the current flags")

    merger   = PdfWriter()
    count    = 0
    failures: list[tuple[str, str]] = []

    for doc in visible:
        if progress_cb is not None:
            progress_cb(doc)
        doc_id = doc.get("doc_id")
        if not doc_id:
            continue
        try:
            pdf_bytes = session.fetch_pdf(doc_id, app_number, pages=doc.get("pages", 1))
            outline = f"[{doc.get('code','?')}] — {doc['doc_type']} ({doc['date']})"
            merger.append(io.BytesIO(pdf_bytes), outline_item=outline)
            count += 1
        except Exception as exc:
            failures.append((doc_id, str(exc)))
        time.sleep(0.2)  # avoid Cloudflare rate-limiting across documents

    # Path C: KOPD re-probe — when ALL EPO Register fetches failed, reset KOPD's
    # reachability cache and try fetching the same docs via KOPD instead.
    if count == 0:
        count = _try_kopd_fallback(visible, app_number, merger, failures)

    if count == 0:
        detail = "; ".join(f"{did}: {err[:80]}" for did, err in failures[:3])
        raise ValueError(f"Could not retrieve any valid PDFs — {detail or 'no docs had doc_id'}")

    out = io.BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)
    return out


def _try_kopd_fallback(
    visible: list[dict],
    app_number: str,
    merger: PdfWriter,
    failures: list[tuple[str, str]],
) -> int:
    """
    Re-probe KOPD and attempt to download the same docs via KOPD when EPO
    Register failed entirely. Returns the number of PDFs successfully fetched.

    Matches EPO Register docs to KOPD docs by (date, doc_type[:25] lowercased).
    Appends matched PDFs to *merger* in place and records failures.
    """
    _kopd._reset_reachable_cache()
    if not _kopd.is_reachable():
        return 0

    print(
        f"  [epo→kopd fallback] EPO Register returned 0 PDFs — retrying via KOPD",
        file=sys.stderr,
    )
    try:
        kopd_docs = _kopd.list_documents(app_number)
    except Exception as exc:
        print(f"  [epo→kopd fallback] KOPD doclist failed: {exc}", file=sys.stderr)
        return 0

    # Build lookup: (date, first-25-chars-of-doc_type-lowercased) → kopd doc
    kopd_map: dict[tuple[str, str], dict] = {}
    for kd in kopd_docs:
        key = (kd["date"], kd["doc_type"][:25].lower().strip())
        kopd_map[key] = kd

    count = 0
    for doc in visible:
        key = (doc.get("date", ""), doc.get("doc_type", "")[:25].lower().strip())
        kopd_doc = kopd_map.get(key)
        if not kopd_doc:
            failures.append((doc.get("doc_id", "?"), "kopd: no matching doc by date+type"))
            continue
        try:
            pdf_bytes = _kopd.fetch_doc_pdf(kopd_doc)
            outline = f"[{doc.get('code','?')}] — {doc['doc_type']} ({doc['date']})"
            merger.append(io.BytesIO(pdf_bytes), outline_item=outline)
            count += 1
        except Exception as exc:
            failures.append((doc.get("doc_id", "?"), f"kopd: {exc}"))
        time.sleep(0.3)

    print(
        f"  [epo→kopd fallback] fetched {count}/{len(visible)} docs via KOPD",
        file=sys.stderr,
    )
    return count


def doc_fingerprint(docs: list[dict]) -> str:
    """
    16-char SHA-256 over sorted (doc_id, date) pairs — used by the CLI manifest
    to skip re-downloads when the document set hasn't changed.
    """
    key = "|".join(
        sorted(f"{d.get('doc_id','')}_{d.get('date','')}" for d in docs)
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]

"""
ep/pdf.py — Session-aware PDF merger for EP register documents
==============================================================

Fetches each document's PDF via a shared RegisterSession (cookie warmup once,
reuse for every download in the bundle) and merges them into one output PDF
with bookmarks labelled "[CODE] — doc_type (YYYY-MM-DD)".

Zero-miss guarantee: three-pass fetch strategy per document:

  Pass 1 — EPO Register (all docs in sequence).
  Pass 2 — EPO Register per-doc retry (3 attempts, 10s/30s/60s waits,
            fresh session each time) for any doc that failed in pass 1.
  Pass 3 — KOPD per-doc fallback (match by date + doc_type[:25]) for
            anything still failing after pass 2.

Raises ValueError with failure detail if any doc cannot be retrieved after
all three passes — never writes a partial PDF.

Exposed API:
  - merge_bundle_pdfs(session, bundle, app_number, *, show_extra, show_intclaim)
  - doc_fingerprint(docs) — for manifest-based change detection
"""

from __future__ import annotations

import hashlib
import io
import sys
import time

import requests
from PyPDF2 import PdfWriter

from . import bundles as _bundles
from . import kopd_client as _kopd
from .register_client import RegisterSession, _BROWSER_HEADERS


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

    Three-pass strategy (see module docstring). Raises ValueError if any doc
    cannot be retrieved — never returns a partial result.

    bundle: a dict with "documents" (list) and "type" keys (see ep.bundles).
    app_number: EP application number (digits only or 'EP' prefix — both ok).
    progress_cb: optional callable(doc) invoked before each PDF download.
    """
    visible = _bundles._filter_docs(
        bundle["documents"], show_extra=show_extra, show_intclaim=show_intclaim
    )
    if not visible:
        raise ValueError("No documents in this bundle with the current flags")

    merger = PdfWriter()

    # Pass 1 — EPO Register, all docs
    pending: list[dict] = []
    for doc in visible:
        if progress_cb is not None:
            progress_cb(doc)
        doc_id = doc.get("doc_id")
        if not doc_id:
            continue
        try:
            pdf_bytes = session.fetch_pdf(doc_id, app_number, pages=doc.get("pages", 1))
            _append(merger, pdf_bytes, doc)
        except Exception as exc:
            print(
                f"  [pdf] pass-1 fail for {doc_id} ({doc.get('doc_type','?')[:40]}): "
                f"{exc} — will retry",
                file=sys.stderr,
            )
            pending.append(doc)
        time.sleep(0.2)

    # Pass 2 — EPO Register per-doc retry (3 attempts, increasing wait)
    still_failed: list[dict] = []
    for doc in pending:
        doc_id = doc["doc_id"]
        fetched = False
        for attempt, wait in enumerate((10, 30, 60)):
            print(
                f"  [pdf] EPO retry {attempt + 1}/3 for {doc_id} — {wait}s wait",
                file=sys.stderr,
            )
            time.sleep(wait)
            session.reset()
            try:
                pdf_bytes = session.fetch_pdf(doc_id, app_number, pages=doc.get("pages", 1))
                _append(merger, pdf_bytes, doc)
                fetched = True
                print(
                    f"  [pdf] EPO retry {attempt + 1} succeeded for {doc_id}",
                    file=sys.stderr,
                )
                break
            except Exception as exc:
                print(
                    f"  [pdf] EPO retry {attempt + 1} failed for {doc_id}: {exc}",
                    file=sys.stderr,
                )
        if not fetched:
            still_failed.append(doc)

    # Pass 3 — KOPD per-doc fallback for anything still failing
    if still_failed:
        still_failed = _pass3_kopd(still_failed, app_number, merger)

    if still_failed:
        detail = "; ".join(
            f"{d.get('doc_id','?')} ({d.get('doc_type','?')[:40]})"
            for d in still_failed
        )
        raise ValueError(
            f"Failed to retrieve {len(still_failed)}/{len(visible)} doc(s) "
            f"after EPO ×3 + KOPD: {detail}"
        )

    out = io.BytesIO()
    merger.write(out)
    merger.close()
    out.seek(0)
    return out


def _append(merger: PdfWriter, pdf_bytes: bytes, doc: dict) -> None:
    outline = f"[{doc.get('code', '?')}] — {doc['doc_type']} ({doc['date']})"
    merger.append(io.BytesIO(pdf_bytes), outline_item=outline)


def _pass3_kopd(
    failed_docs: list[dict],
    app_number: str,
    merger: PdfWriter,
) -> list[dict]:
    """
    Try each failed doc via KOPD, matched by (date, doc_type[:25]).
    Returns a list of docs that KOPD also could not supply.
    """
    _kopd._reset_reachable_cache()
    if not _kopd.is_reachable():
        print(
            f"  [pdf] KOPD unreachable — {len(failed_docs)} doc(s) unrecoverable",
            file=sys.stderr,
        )
        return failed_docs

    print(
        f"  [pdf] KOPD pass-3 for {len(failed_docs)} failed doc(s)",
        file=sys.stderr,
    )
    try:
        kopd_docs = _kopd.list_documents(app_number)
    except Exception as exc:
        print(f"  [pdf] KOPD doclist failed: {exc}", file=sys.stderr)
        return failed_docs

    # (date, first-25-chars-of-doc_type-lowercased) → kopd doc
    kopd_map: dict[tuple[str, str], dict] = {}
    for kd in kopd_docs:
        key = (kd["date"], kd["doc_type"][:25].lower().strip())
        kopd_map[key] = kd

    permanent: list[dict] = []
    for doc in failed_docs:
        key = (doc.get("date", ""), doc.get("doc_type", "")[:25].lower().strip())
        kopd_doc = kopd_map.get(key)
        if not kopd_doc:
            print(
                f"  [pdf] KOPD: no match for {doc.get('doc_id','?')} "
                f"({doc.get('date','')} / {doc.get('doc_type','?')[:40]})",
                file=sys.stderr,
            )
            permanent.append(doc)
            continue
        try:
            pdf_bytes = _kopd.fetch_doc_pdf(kopd_doc)
            _append(merger, pdf_bytes, doc)
            print(
                f"  [pdf] KOPD recovered {doc.get('doc_id','?')} "
                f"({doc.get('doc_type','?')[:40]})",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"  [pdf] KOPD failed for {doc.get('doc_id','?')}: {exc}",
                file=sys.stderr,
            )
            permanent.append(doc)
        time.sleep(0.3)

    return permanent


def doc_fingerprint(docs: list[dict]) -> str:
    """
    16-char SHA-256 over sorted (doc_id, date) pairs — used by the CLI manifest
    to skip re-downloads when the document set hasn't changed.
    """
    key = "|".join(
        sorted(f"{d.get('doc_id', '')}_{d.get('date', '')}" for d in docs)
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]

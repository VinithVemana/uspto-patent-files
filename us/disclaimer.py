"""
us/disclaimer.py — Terminal Disclaimer parsing pipeline.

Pipeline (production-grade, GPT-backed):

  1. Pull all docs from ``_get_documents(app_no)`` and keep anything that
     looks like a Terminal Disclaimer document. We accept three signals:
       a. ``code`` is one of {"DISQ", "DIST"} (the canonical examiner
          decision and applicant filing codes).
       b. ``code`` starts with one of those prefixes followed by ``.``
          (covers the e-file variants ``DIST.E.FILE`` and ``DISQ.E.FILE``
          that the USPTO API has started returning for some applications).
       c. The document description contains the words "terminal disclaimer"
          (case-insensitive), to catch idiosyncratic codes (e.g. legacy
          PALM codes from pre-2010 applications) so we never miss a TD.

  2. Each matching PDF is downloaded once into ``<save_dir>/`` (typically the
     main patent's folder under ``td_source/``). Subsequent runs reuse the
     cached PDF, OCR text, and LLM classification — no network or LLM
     traffic on a clean re-run.

  3. Each PDF is OCRed (``pdftoppm`` + ``tesseract``) and the OCR text is
     cached in ``<save_dir>/<basename>.ocr.txt``.

  4. Every OCR text is sent to ``us.llm_disclaimer.classify_document``,
     which asks GPT-4o-mini for a structured ``{doc_type, approved,
     patents}`` JSON. The result is cached in ``<save_dir>/<basename>.llm.json``.

  5. Documents are paired chronologically — DIST filings list patents,
     DISQ reviews carry the approved/disapproved decision. Each DISQ
     consumes all DIST filings that came in before it (since the previous
     DISQ). The result is a list of decisions compatible with the
     historical ``get_disq_decisions`` shape:

       [
         {"date": ISO, "approved": bool|None,
          "patents": [digits, ...],         # union of paired DIST patents
          "pdf_url": str,                   # the DISQ's URL (or DIST if no DISQ)
          "code":    str,                   # DISQ / DIST / ...
          "sources": list[str],             # all PDFs feeding this decision
         },
         ...
       ]

System binaries required: ``pdftoppm`` (poppler) and ``tesseract``. Both
must be on PATH (``brew install poppler tesseract`` on macOS). The OpenAI
key must be set as ``OPENAPI_KEY`` (or ``OPENAI_API_KEY``) in the env or
``.env``; without it, classification falls back to a regex-only heuristic
that is intentionally conservative (no patents extracted, approval=None)
so callers fail closed rather than silently mis-handling disclaimers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

import requests

from .config import HEADERS
from .client import _get_documents
from . import llm_disclaimer


# Canonical doc-code prefixes for Terminal Disclaimer paperwork.
#   DISQ — Terminal Disclaimer Review Decision (examiner)
#   DIST — Terminal Disclaimer (applicant filing)
# USPTO sometimes appends e-filing suffixes, e.g. ``DIST.E.FILE``.
_TD_CODE_PREFIXES = ("DISQ", "DIST")
_TD_DESC_RE = re.compile(r"terminal\s+disclaimer", re.IGNORECASE)


def _is_td_doc(doc: dict) -> bool:
    """True if ``doc`` is a Terminal Disclaimer-related document."""
    code = (doc.get("code") or "").upper()
    desc = doc.get("desc") or ""
    for pfx in _TD_CODE_PREFIXES:
        if code == pfx or code.startswith(pfx + "."):
            return True
    if _TD_DESC_RE.search(desc):
        return True
    return False


def _classify_code(code: str) -> str:
    """Return the canonical doc family — 'DISQ', 'DIST', or 'OTHER'."""
    code = (code or "").upper()
    for pfx in _TD_CODE_PREFIXES:
        if code == pfx or code.startswith(pfx + "."):
            return pfx
    return "OTHER"


# ---------------------------------------------------------------------------
# PDF download + OCR (with on-disk caching)
# ---------------------------------------------------------------------------

def _safe_basename(doc: dict) -> str:
    """Filesystem-safe basename derived from doc date + code + identifier."""
    date = (doc.get("date") or "")[:10] or "unknown"
    code = (doc.get("code") or "UNK").replace(os.sep, "_")
    # Pull a short hash from the URL so multiple same-day-same-code docs
    # don't collide.
    url = doc.get("pdf_url") or ""
    suffix = ""
    if url:
        m = re.search(r"([A-Z0-9]{6,})\.pdf", url)
        if m:
            suffix = f"_{m.group(1)[:10]}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{date}_{code}{suffix}")
    return safe or "td_doc"


def _download_pdf(url: str, dest_path: str) -> bool:
    """Idempotent PDF download. Returns True iff dest exists with content."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        return True
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
    except Exception as exc:
        print(f"  [TD download] {url}: {exc}", file=sys.stderr)
        return False
    try:
        with open(dest_path, "wb") as fh:
            fh.write(r.content)
    except OSError as exc:
        print(f"  [TD download] write failed for {dest_path}: {exc}", file=sys.stderr)
        return False
    return os.path.getsize(dest_path) > 0


def _ocr_pdf_to_text(pdf_path: str) -> str:
    """Run pdftoppm + tesseract on a local PDF. Returns concatenated text."""
    if not os.path.exists(pdf_path):
        return ""

    with tempfile.TemporaryDirectory() as td:
        try:
            subprocess.run(
                ["pdftoppm", "-r", "300", pdf_path, os.path.join(td, "p"), "-png"],
                check=True, capture_output=True,
            )
        except FileNotFoundError:
            print("  [TD OCR] pdftoppm not on PATH — install poppler "
                  "(brew install poppler)", file=sys.stderr)
            return ""
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", "ignore")[:300]
            print(f"  [TD OCR] pdftoppm failed: {stderr}", file=sys.stderr)
            return ""

        chunks: list[str] = []
        for png in sorted(f for f in os.listdir(td) if f.endswith(".png")):
            png_path = os.path.join(td, png)
            try:
                out = subprocess.run(
                    ["tesseract", png_path, "-"],
                    check=True, capture_output=True, text=True,
                )
                chunks.append(out.stdout)
            except FileNotFoundError:
                print("  [TD OCR] tesseract not on PATH — install "
                      "(brew install tesseract)", file=sys.stderr)
                return ""
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "")[:300]
                print(f"  [TD OCR] tesseract failed on {png}: {stderr}",
                      file=sys.stderr)
        return "\n".join(chunks)


def _ocr_with_cache(pdf_path: str) -> str:
    """OCR ``pdf_path`` with a side-by-side ``.ocr.txt`` cache."""
    cache = pdf_path + ".ocr.txt"
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        try:
            with open(cache, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            pass
    text = _ocr_pdf_to_text(pdf_path)
    if text:
        try:
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            print(f"  [TD OCR] cache write failed for {cache}: {exc}",
                  file=sys.stderr)
    return text


def _classify_with_cache(pdf_path: str, ocr_text: str, log_label: str) -> dict:
    """LLM-classify ``ocr_text`` with a ``.llm.json`` cache next to ``pdf_path``."""
    cache = pdf_path + ".llm.json"
    if os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
            if isinstance(cached, dict) and "doc_type" in cached:
                print(f"  [LLM TD] [{log_label}] cached → {cached['doc_type']} "
                      f"approved={cached.get('approved')} "
                      f"patents={cached.get('patents')}",
                      file=sys.stderr)
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    parsed = llm_disclaimer.classify_document(ocr_text, log_label=log_label)
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(parsed, fh, indent=2)
    except OSError as exc:
        print(f"  [TD LLM] cache write failed for {cache}: {exc}",
              file=sys.stderr)
    return parsed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def get_terminal_disclaimer_decisions(
    app_no: str,
    save_dir: Optional[str] = None,
) -> list[dict]:
    """
    Find every TD document on ``app_no``, OCR + LLM-classify each, and pair
    DIST filings with the DISQ decisions that follow them.

    ``save_dir``: directory to persist source PDFs (and OCR / LLM caches).
    Created if missing. ``None`` falls back to a private temp dir; callers
    that want auditable artifacts should always pass an explicit folder
    (typically the main patent's ``<root>/US{patent_no}/td_source/``).

    Returns a list of decision dicts, one per DISQ found, in the order they
    were issued (most recent last) — empty list when no TD docs exist:

        {
          "date":     str,                # ISO date of the DISQ (or DIST when unpaired)
          "approved": bool | None,        # examiner's decision; None if undetermined
          "patents":  list[str],          # union of digit-only patents from paired DISTs
          "pdf_url":  str,                # the DISQ's URL (or DIST if no DISQ)
          "code":     str,                # 'DISQ' / 'DIST' / raw code
          "sources":  list[str],          # local PDF paths feeding this decision
        }

    The returned shape is intentionally backward-compatible with what
    ``bundles_api._process_disclaimers`` already consumes (filters
    ``approved is True`` and reads ``patents``).
    """
    docs = _get_documents(app_no)
    td_docs = [d for d in docs if _is_td_doc(d)]
    if not td_docs:
        return []

    # _get_documents returns dates DESC. Pair by ascending date so DISTs
    # appear before the DISQ that reviews them.
    td_docs.sort(key=lambda d: d.get("date") or "")

    # Resolve save_dir.
    own_tempdir: Optional[tempfile.TemporaryDirectory] = None
    if save_dir is None:
        own_tempdir = tempfile.TemporaryDirectory(prefix="td_source_")
        save_dir = own_tempdir.name
    else:
        os.makedirs(save_dir, exist_ok=True)

    print(f"\n  Terminal Disclaimer docs found: {len(td_docs)} "
          f"(saving to {save_dir})", file=sys.stderr)
    for d in td_docs:
        print(f"    {(d.get('date') or '')[:10]}  {d.get('code'):<14} "
              f"{(d.get('desc') or '')[:60]}", file=sys.stderr)

    parsed_docs: list[dict] = []
    for d in td_docs:
        url = d.get("pdf_url") or ""
        if not url:
            print(f"  [TD] no pdf_url for {d.get('code')} {d.get('date')[:10]} "
                  f"— skipping", file=sys.stderr)
            continue

        basename = _safe_basename(d)
        pdf_path = os.path.join(save_dir, basename + ".pdf")
        ok = _download_pdf(url, pdf_path)
        if not ok:
            continue

        log_label = f"{d.get('code')} {(d.get('date') or '')[:10]}"
        text = _ocr_with_cache(pdf_path)
        parsed = _classify_with_cache(pdf_path, text, log_label)

        family = _classify_code(d.get("code", ""))
        # Trust the LLM's classification when the doc-code family doesn't
        # cleanly map (e.g. matched only by description). When the code
        # family is clear-cut, also trust the LLM but log mismatches.
        llm_type = parsed.get("doc_type")
        resolved_type = family if family in ("DISQ", "DIST") else (
            "DISQ" if llm_type == "review"
            else "DIST" if llm_type == "filing"
            else "OTHER"
        )
        if family in ("DISQ", "DIST") and llm_type not in (None, "other"):
            expected = "review" if family == "DISQ" else "filing"
            if llm_type != expected:
                print(f"  [TD] {log_label}: code={family} but LLM said "
                      f"{llm_type} — keeping code-derived family",
                      file=sys.stderr)

        parsed_docs.append({
            "date":      d.get("date") or "",
            "code":      d.get("code") or "",
            "desc":      d.get("desc") or "",
            "pdf_url":   url,
            "pdf_path":  pdf_path,
            "type":      resolved_type,        # 'DISQ' | 'DIST' | 'OTHER'
            "approved":  parsed.get("approved"),
            "patents":   parsed.get("patents", []),
            "notes":     parsed.get("notes", ""),
        })

    if own_tempdir is not None:
        # Keep the dir alive only until pairing is done — the caller didn't
        # want a persistent save_dir.
        pass

    decisions = _pair_dist_disq(parsed_docs)

    if own_tempdir is not None:
        own_tempdir.cleanup()

    return decisions


def _pair_dist_disq(parsed_docs: list[dict]) -> list[dict]:
    """
    Walk ``parsed_docs`` (chronological ascending) and emit one decision per
    DISQ, attributing each DISQ the patents from every DIST that arrived
    since the previous DISQ.

    Unpaired DISTs (no later DISQ) are emitted with approved=None so the
    caller can see them but won't act on them — they're conservatively
    treated as "pending review".
    """
    decisions: list[dict] = []
    pending_dist: list[dict] = []

    for p in parsed_docs:
        if p["type"] == "DIST":
            pending_dist.append(p)
            continue
        if p["type"] == "DISQ":
            patents: list[str] = []
            seen: set[str] = set()
            sources: list[str] = []
            for d in pending_dist:
                sources.append(d["pdf_path"])
                for pn in d.get("patents", []):
                    if pn not in seen:
                        seen.add(pn)
                        patents.append(pn)
            sources.append(p["pdf_path"])

            # If the DISQ itself surfaced patents (rare — combined forms),
            # union them in too.
            for pn in p.get("patents", []):
                if pn not in seen:
                    seen.add(pn)
                    patents.append(pn)

            decisions.append({
                "date":     p["date"],
                "approved": p.get("approved"),
                "patents":  patents,
                "pdf_url":  p["pdf_url"],
                "code":     p["code"],
                "sources":  sources,
            })
            pending_dist = []
            continue

        # OTHER — keep its patents around to roll into the next DISQ, but
        # don't emit a standalone decision.
        if p.get("patents"):
            pending_dist.append(p)

    # Any DIST left over without a matching DISQ → unpaired, conservatively
    # surface with approved=None (caller will treat as not-approved).
    for d in pending_dist:
        decisions.append({
            "date":     d["date"],
            "approved": None,
            "patents":  d.get("patents", []),
            "pdf_url":  d["pdf_url"],
            "code":     d["code"],
            "sources":  [d["pdf_path"]],
        })

    return decisions


# ---------------------------------------------------------------------------
# Backward-compatible alias
# ---------------------------------------------------------------------------

def get_disq_decisions(app_no: str, save_dir: Optional[str] = None) -> list[dict]:
    """Legacy name for :func:`get_terminal_disclaimer_decisions`. Identical."""
    return get_terminal_disclaimer_decisions(app_no, save_dir=save_dir)

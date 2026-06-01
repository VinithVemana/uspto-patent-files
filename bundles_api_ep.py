"""
bundles_api_ep.py — EP (European Patent) prosecution-bundle CLI
===============================================================

Mirrors bundles_api.py for USPTO. Core logic lives in the `ep/` module so the
FastAPI server can import it directly.

INPUT FORMATS
-------------
  EP application number   10173239            or  EP10173239
  Formatted with dot      10173239.4          check digit stripped
  EP publication number   EP3456789           or  EP3456789A1 / B1
  Bare 7-digit pub        3456789             ambiguous — tries as pub first
  WO/PCT publication      WO2015077217        or  WO2015/077217 / PCT/...

BULK MODE
---------
Pass multiple patents as space-, comma-, or pipe-separated values.
Each patent gets its own subfolder inside --output-dir.

    python bundles_api_ep.py EP2919231B1 --download --output-dir ./bulk
    python bundles_api_ep.py "EP2420929,EP2985974,EP3456789B1" --download --output-dir ./bulk
    python bundles_api_ep.py "EP2420929|EP2985974|EP3456789B1" --download --output-dir ./bulk

RUN FROM THE COMMAND LINE
-------------------------
    python bundles_api_ep.py <number> [options]

    Options (mirror USPTO CLI):
      --text              Human-readable text table (default: JSON)
      --show-extra        Include supporting docs (delivery, receipts, minutes)
      --show-intclaim     Include intermediate claim docs in round bundles
      --download          Download each bundle PDF to disk
      --output-dir DIR    Default: ./EP{app_number}/
      --separate-bundles  One PDF per prosecution round (default: 3-bundle collapse)
      --list-docs         Just list every document + classification, no download
                          (useful for checking what will land in each bundle)
      --base-url URL      Base URL for download_url links (default: http://localhost:7901)

    Examples:
      python bundles_api_ep.py EP2420929
      python bundles_api_ep.py 10173239 --text
      python bundles_api_ep.py EP2985974 --download --output-dir ./ep_docs
      python bundles_api_ep.py WO2015077217 --text
      python bundles_api_ep.py EP2420929 --list-docs            # dry-run listing

      # Bulk download — space, comma, or pipe separated; each gets its own subfolder
      python bundles_api_ep.py EP2420929 EP2985974 --download --output-dir ./bulk
      python bundles_api_ep.py "EP2420929,EP2985974" --download --output-dir ./bulk

WEB SERVER
----------
    uvicorn bundles_server:app --host 0.0.0.0 --port 7901
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from tqdm import tqdm

from ep import bundles as ep_bundles
from ep import config as ep_config
from ep import kopd_client, ops_client, pdf as ep_pdf, resolver
from ep.register_client import RegisterSession
from us import pcs_api


# ===========================================================================
# Shared helpers (metadata fetch, manifest)
# ===========================================================================

MANIFEST_FILE = "manifest.json"


def _fetch_doclist(app_no: str, session: RegisterSession) -> tuple[list[dict], str]:
    """
    Fetch the prosecution doc list: EPO Register primary, KOPD fallback,
    EPO Register last-resort retry if KOPD also fails.

    Tags every doc with ``_source: "epo"`` or ``_source: "kopd"``.
    Raises if all attempts fail.
    """
    epo_exc: Exception | None = None

    # ── Attempt 1: EPO Register ───────────────────────────────────────────────
    try:
        docs = session.list_documents(f"EP{app_no}")
        if docs:
            for d in docs:
                d["_source"] = "epo"
            return docs, "epo"
        print("  [epo] returned empty list — falling back to KOPD", file=sys.stderr)
    except Exception as exc:
        epo_exc = exc
        print(f"  [epo] doclist error ({exc}) — falling back to KOPD", file=sys.stderr)

    # ── Attempt 2: KOPD ───────────────────────────────────────────────────────
    kopd_exc: Exception | None = None
    if kopd_client.is_reachable():
        try:
            docs = kopd_client.list_documents(app_no)
            if docs:
                for d in docs:
                    d["_source"] = "kopd"
                return docs, "kopd"
            print("  [kopd] returned empty list", file=sys.stderr)
        except Exception as exc:
            kopd_exc = exc
            print(f"  [kopd] failed ({exc}) — retrying EPO Register after 30s",
                  file=sys.stderr)
    else:
        print("  [kopd] unreachable — retrying EPO Register after 30s", file=sys.stderr)

    # ── Attempt 3: EPO Register again (long wait, fresh session) ─────────────
    # Both EPO and KOPD failed or returned empty. Wait 30s and try EPO one more
    # time — CF rate-limit windows are usually shorter than 30s.
    time.sleep(30)
    print(f"  [epo] last-resort retry for EP{app_no} ...", file=sys.stderr)
    try:
        # Force a brand-new session to clear any CF-poisoned cookies.
        from ep.register_client import RegisterSession as _RS
        fresh = _RS()
        docs = fresh.list_documents(f"EP{app_no}")
        if docs:
            for d in docs:
                d["_source"] = "epo"
            session._s = fresh._s          # absorb the warmed session
            session._warmed_for = fresh._warmed_for
            return docs, "epo"
        raise RuntimeError(f"EPO Register last-resort also returned empty for {app_no}")
    except Exception as exc:
        combined = (
            f"EPO Register: {epo_exc or 'empty'}; "
            f"KOPD: {kopd_exc or 'empty/unreachable'}; "
            f"EPO last-resort: {exc}"
        )
        raise RuntimeError(f"All doclist sources failed for {app_no}: {combined}") from exc


def _fetch_meta_and_doclist(
    app_no: str, pub_no: str | None, session: RegisterSession,
) -> tuple[dict, list[dict]]:
    """
    Given a pre-resolved (app_no, pub_no), fetch OPS biblio metadata and the
    prosecution doclist (KOPD primary, EPO Register fallback). No resolve step.

    Returns (meta, documents). Each doc dict is tagged with `_source`.
    Reused by `_fetch_everything` (main patent) and the divisional ancestor
    walker so we don't re-resolve identifiers we already have.
    """
    pub_biblio = ops_client.get_publication_biblio(f"EP{pub_no}") if pub_no else None
    reg_biblio = ops_client.get_register_biblio(f"EP{pub_no}") if pub_no else None

    meta: dict
    if pub_biblio:
        meta = ops_client.extract_metadata(pub_biblio, reg_biblio)
    else:
        meta = {
            "application_number": app_no,
            "publication_number": pub_no,
            "patent_number": None,
            "title": "N/A",
            "status": "N/A",
            "filing_date": "",
            "publication_date": "",
            "grant_date": None,
            "inventors": [],
            "applicants": [],
            "ipc_codes": [],
        }
    meta["application_number"] = app_no  # resolver's value wins

    documents, doc_source = _fetch_doclist(app_no, session)
    print(f"Found {len(documents)} documents for EP{app_no} (source: {doc_source}).",
          file=sys.stderr)
    documents = _dedup_same_date_amended_claims(documents)
    return meta, documents


def _dedup_same_date_amended_claims(docs: list[dict]) -> list[dict]:
    """
    Drop exact-duplicate amended-claims docs: same date AND same doc_type.
    Different doc types on the same date (e.g. 'Amended claims filed after
    receipt of...' + 'Amended claims with annotations') are NOT deduplicated —
    they are distinct documents (clean version vs. redline).
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    dropped = 0
    for d in docs:
        doc_type = (d.get("doc_type") or "").strip().lower()
        if doc_type.startswith("amended claims"):
            key = (d.get("date") or "", doc_type)
            if key in seen:
                print(f"  [dedup] skipping exact-duplicate amended-claims doc on "
                      f"{d.get('date','')}: {d.get('doc_type','')[:60]}", file=sys.stderr)
                dropped += 1
                continue
            seen.add(key)
        out.append(d)
    if dropped:
        print(f"  [dedup] dropped {dropped} exact-duplicate amended-claims doc(s)",
              file=sys.stderr)
    return out


def _fetch_everything(input_number: str) -> tuple[str, str | None, dict, list[dict], RegisterSession]:
    """
    Resolve + fetch metadata + document list for an EP patent.

    Returns (app_number, pub_number, metadata, documents, register_session).
    Raises ValueError / RuntimeError on unresolvable input or fetch failure.
    """
    print(f"Resolving {input_number} ...", file=sys.stderr)
    app_no, pub_no = resolver.resolve(input_number)
    print(f"EP application number: EP{app_no}" +
          (f"  (publication EP{pub_no})" if pub_no else ""), file=sys.stderr)

    session = RegisterSession()
    meta, documents = _fetch_meta_and_doclist(app_no, pub_no, session)
    return app_no, pub_no, meta, documents, session


# ---------------------------------------------------------------------------
# Manifest — skip-unchanged on re-download (mirrors USPTO behaviour)
# ---------------------------------------------------------------------------

def _load_manifest(output_dir: str) -> dict:
    path = os.path.join(output_dir, MANIFEST_FILE)
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_manifest(output_dir: str, app_no: str,
                   artifacts: dict, failures: list | None = None) -> None:
    path = os.path.join(output_dir, MANIFEST_FILE)
    payload: dict = {
        "jurisdiction": "EP",
        "app_no":       app_no,
        "saved_at":     datetime.now(timezone.utc).isoformat(),
        "artifacts":    {
            k: {"filename": v["filename"], "fingerprint": v["fingerprint"]}
            for k, v in artifacts.items()
            if "filename" in v and "fingerprint" in v
        },
    }
    if failures:
        payload["failures"] = failures
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def _needs_download(key: str, filename: str, fingerprint: str,
                    manifest: dict, output_dir: str) -> tuple[bool, str]:
    filepath = os.path.join(output_dir, filename)
    if not os.path.exists(filepath):
        return True, "missing"
    prev = manifest.get("artifacts", {}).get(key)
    if not prev:
        return True, "not in manifest"
    if prev.get("filename") != filename:
        return True, f"renamed (was {prev['filename']})"
    if prev.get("fingerprint") != fingerprint:
        return True, "documents updated"
    return False, "up-to-date"


# ===========================================================================
# CLI output (JSON / text / list-docs)
# ===========================================================================

def _print_metadata_header(meta: dict) -> None:
    print("=" * 64)
    print(f"Title:         {meta.get('title','N/A')}")
    print(f"Status:        {meta.get('status','N/A')}")
    print(f"Filing date:   {meta.get('filing_date','') or 'N/A'}")
    print(f"App no.:       EP{meta.get('application_number','?')}")
    print(f"Pub no.:       EP{meta.get('publication_number','?')}"
          + (f"  {meta.get('kind_code','')}" if meta.get("kind_code") else ""))
    if meta.get("grant_date"):
        print(f"Grant date:    {meta['grant_date']}")
    ipc = meta.get("ipc_codes") or []
    if ipc:
        print(f"IPC:           {', '.join(ipc[:6])}")
    print(f"Inventors:     {', '.join(i['name'] for i in meta.get('inventors', [])) or 'N/A'}")
    print(f"Applicants:    {', '.join(meta.get('applicants', [])) or 'N/A'}")
    print("=" * 64)


def _cmd_list_docs(meta: dict, documents: list[dict]) -> None:
    """Dry-run: show every document with its classification, no download."""
    _print_metadata_header(meta)
    print(f"\nTotal documents: {len(documents)}\n")
    print(f"{'Date':<12} {'Code':<8} {'Tier':<10} {'Procedure':<30} Type")
    print("-" * 120)
    for d in documents:
        code = ep_config.short_code(d["doc_type"])
        tier = ep_config.classify(d["doc_type"], bundle_type="round")  # ambiguous position
        proc = d.get("procedure", "")[:28]
        dtype = d["doc_type"][:60]
        print(f"{d['date']:<12} {code:<8} {tier:<10} {proc:<30} {dtype}")


# ===========================================================================
# Download orchestration (for --download)
# ===========================================================================

def _bundle_planned_fingerprint(b: dict) -> str:
    """
    Tag a non-granted bundle's fingerprint with its doclist source so source
    swaps (KOPD ↔ EPO) invalidate the manifest correctly. KOPD-sourced
    bundles get a `kopd:` prefix; EPO-sourced bundles use the bare 16-hex
    fingerprint (unchanged from legacy behaviour).
    """
    fp_hex = ep_pdf.doc_fingerprint(b["documents"])
    src = b["documents"][0].get("_source") if b["documents"] else None
    return f"kopd:{fp_hex}" if src == "kopd" else fp_hex


def _granted_claims_planned_fingerprint(
    b: dict, pub_no: str | None,
) -> str:
    """
    Predict the fingerprint `_build_granted_claims_pdf` will assign, so
    `_needs_download` can detect source swaps (PCS ↔ KOPD ↔ EPO) and force
    a re-fetch when the source changes. Mirrors the US-side helper
    `bundles_api._granted_claims_planned_fingerprint`.

    kind_code may be "A1" (pre-grant) from OPS biblio — PCS will probe
    B2/B1/B3 automatically, so any pub_no is sufficient for PCS eligibility.
    """
    if pub_no and pcs_api.is_reachable():
        # Use "B?" as placeholder — actual resolved kind code baked in at save time.
        return f"pcs:EP-{pub_no}-B?"
    return _bundle_planned_fingerprint(b)


def _build_granted_claims_pdf(
    b: dict,
    pub_no: str | None, kind_code: str | None, grant_date: str | None,
    session: RegisterSession, app_no: str,
    filepath: str,
    log_label: str = "Granted_claims",
) -> tuple[bool, str, str, str]:
    """
    Granted-claims builder with three-source chain: PCS → KOPD → EPO Register.

    Returns ``(ok, source, fingerprint, reason)``:
      source      ∈ {"pcs", "kopd", "epo", ""}
      fingerprint = "pcs:EP-{pub_no}-{kind_code}" / "kopd:{hex}" / 16-hex / ""
      reason      = "ok" on success, error string on failure
    """
    # Step 1 — try PCS (probes B2/B1/B3 regardless of OPS kind_code; only needs pub_no)
    if pub_no and pcs_api.is_reachable():
        print(f"  [{log_label}] PCS reachable, probing B2/B1/B3 for EP{pub_no}",
              file=sys.stderr)
        xml, resolved_kc = pcs_api.fetch_claims_xml_ep(pub_no, kind_code)
        if xml is not None:
            buf, render_reason = pcs_api._render_from_xml(
                xml, f"EP{pub_no}{resolved_kc}", grant_date
            )
            if buf is not None:
                try:
                    with open(filepath, "wb") as fh:
                        fh.write(buf.getvalue())
                    size_kb = os.path.getsize(filepath) // 1024
                    print(f"  [{log_label}] -> Saved from pcs_api "
                          f"({size_kb:,} KB, kind={resolved_kc})", file=sys.stderr)
                    return True, "pcs", f"pcs:EP-{pub_no}-{resolved_kc}", "ok"
                except OSError as exc:
                    print(f"  [{log_label}] pcs_api write failed: {exc} — "
                          f"falling back to doclist merge", file=sys.stderr)
            else:
                print(f"  [{log_label}] pcs_api render: {render_reason} — "
                      f"falling back to doclist merge", file=sys.stderr)
        else:
            print(f"  [{log_label}] pcs_api: no match (B2/B1/B3) — "
                  f"falling back to doclist merge", file=sys.stderr)
    elif not pub_no:
        print(f"  [{log_label}] missing pub_no — skipping PCS, "
              f"using doclist merge", file=sys.stderr)

    if not b["documents"]:
        return False, "", "", "no docs in granted bundle and PCS unavailable"

    # Step 2/3 — doclist merge via whichever backend supplied the doclist
    bar = tqdm(total=len(b["documents"]), desc=os.path.basename(filepath),
               file=sys.stderr, leave=False)

    def cb(doc, _bar=bar):
        _bar.set_postfix_str(f"{doc.get('code','?')} {doc['doc_type'][:40]}")
        _bar.update(1)

    docs_src = b["documents"][0].get("_source", "epo")
    fp_hex = ep_pdf.doc_fingerprint(b["documents"])

    if docs_src == "kopd":
        try:
            merged = kopd_client.merge_bundle_pdfs(b, progress_cb=cb)
            bar.close()
            with open(filepath, "wb") as fh:
                fh.write(merged.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  [{log_label}] -> Saved from KOPD ({size_kb:,} KB)",
                  file=sys.stderr)
            return True, "kopd", f"kopd:{fp_hex}", "ok"
        except Exception as exc:
            bar.close()
            return False, "", "", f"KOPD merge failed: {exc}"

    # EPO Register merge (legacy default)
    try:
        merged = ep_pdf.merge_bundle_pdfs(
            session, b, app_no,
            show_extra=False, show_intclaim=False,
            progress_cb=cb,
        )
        bar.close()
        with open(filepath, "wb") as fh:
            fh.write(merged.getvalue())
        size_kb = os.path.getsize(filepath) // 1024
        print(f"  [{log_label}] -> Saved from EPO Register merge ({size_kb:,} KB)",
              file=sys.stderr)
        return True, "epo", fp_hex, "ok"
    except Exception as exc:
        bar.close()
        return False, "", "", str(exc)


def _download_bundles(
    bundles: list[dict], session: RegisterSession, app_no: str, output_dir: str,
    manifest: dict,
    pub_no: str | None = None, kind_code: str | None = None,
    grant_date: str | None = None,
) -> tuple[dict, list[dict]]:
    """Download the 4-bundle collapse. Returns (artifacts_state, failures)."""
    state: dict = {}
    failures: list[dict] = []

    for b in bundles:
        # Granted bundle with no EPO Register docs may still be served by PCS —
        # skip the empty-doc early-exit for "granted" type only.
        if not b["documents"] and b["type"] != "granted":
            continue
        filename = f"{b['filename']}.pdf"
        key      = f"bundle_{b['filename']}"
        filepath = os.path.join(output_dir, filename)

        if b["type"] == "granted":
            fp = _granted_claims_planned_fingerprint(b, pub_no)
        else:
            fp = _bundle_planned_fingerprint(b)

        needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
        if not needed:
            state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
            print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
            continue

        # Granted bundle: PCS-first, EPO-fallback path.
        if b["type"] == "granted":
            print(f"  [{filename}] {reason} — building (PCS first, EPO fallback)",
                  file=sys.stderr)
            ok, source, fp_actual, fail_reason = _build_granted_claims_pdf(
                b, pub_no, kind_code, grant_date, session, app_no, filepath,
                log_label=filename,
            )
            if ok:
                state[key] = {"filename": filename, "fingerprint": fp_actual, "needed": True}
            else:
                print(f"    -> Failed: {fail_reason}", file=sys.stderr)
                failures.append({"key": key, "filename": filename, "reason": fail_reason})
            continue

        # Other bundles: dispatch on per-doc source (KOPD or EPO Register).
        docs_src = b["documents"][0].get("_source", "epo")
        print(f"  [{filename}] {reason} — downloading {len(b['documents'])} docs "
              f"via {docs_src.upper()}", file=sys.stderr)
        bar = tqdm(total=len(b["documents"]), desc=filename, file=sys.stderr, leave=False)

        def cb(doc, _bar=bar):
            _bar.set_postfix_str(f"{doc.get('code','?')} {doc['doc_type'][:40]}")
            _bar.update(1)

        try:
            if docs_src == "kopd":
                merged = kopd_client.merge_bundle_pdfs(b, progress_cb=cb)
            else:
                merged = ep_pdf.merge_bundle_pdfs(
                    session, b, app_no,
                    show_extra=False, show_intclaim=False,
                    progress_cb=cb,
                )
            bar.close()
            with open(filepath, "wb") as fh:
                fh.write(merged.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"    -> Saved ({size_kb:,} KB)", file=sys.stderr)
            state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
        except Exception as exc:
            bar.close()
            print(f"    -> Failed: {exc}", file=sys.stderr)
            failures.append({"key": key, "filename": filename, "reason": str(exc)})

    return state, failures


def _download_separate(
    bundles_list: list[dict], session: RegisterSession, app_no: str,
    output_dir: str, manifest: dict,
    show_extra: bool, show_intclaim: bool,
) -> tuple[dict, list[dict]]:
    """Download each bundle as a separate PDF."""
    state: dict = {}
    failures: list[dict] = []

    for bundle in bundles_list:
        visible = ep_bundles._filter_docs(
            bundle["documents"], show_extra=show_extra, show_intclaim=show_intclaim
        )
        if not visible:
            continue
        safe = re.sub(r"[^\w\s\-]", "", bundle["label"]).strip().replace(" ", "_")
        filename = f"{safe}.pdf"
        key      = f"sep_bundle_{bundle['index']}"
        fp       = ep_pdf.doc_fingerprint(visible)
        needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
        if not needed:
            state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
            print(f"    [{filename}] up-to-date — skipped", file=sys.stderr)
            continue

        print(f"    [{filename}] {reason} — downloading {len(visible)} docs", file=sys.stderr)
        bar = tqdm(total=len(visible), desc=filename[:40], file=sys.stderr, leave=False)

        def cb(doc, _bar=bar):
            _bar.set_postfix_str(f"{doc.get('code','?')} {doc['doc_type'][:40]}")
            _bar.update(1)

        try:
            merged = ep_pdf.merge_bundle_pdfs(
                session, bundle, app_no,
                show_extra=show_extra, show_intclaim=show_intclaim,
                progress_cb=cb,
            )
            bar.close()
            filepath = os.path.join(output_dir, filename)
            with open(filepath, "wb") as fh:
                fh.write(merged.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"    -> Saved ({size_kb:,} KB)", file=sys.stderr)
            state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
        except Exception as exc:
            bar.close()
            print(f"    -> Failed: {exc}", file=sys.stderr)
            failures.append({"key": key, "filename": filename, "reason": str(exc)})

    return state, failures


def _finalize_manifest(output_dir: str, app_no: str, state: dict, failures: list) -> None:
    downloaded = sum(1 for v in state.values() if v.get("needed"))
    skipped    = sum(1 for v in state.values() if not v.get("needed"))
    failed     = len(failures)
    if not state and not failures:
        return
    _save_manifest(output_dir, app_no, state, failures)
    summary = f"\nSummary: {downloaded} downloaded, {skipped} skipped"
    if failed:
        summary += f", {failed} failed"
        for f in failures:
            summary += f"\n  - {f['filename']}: {f['reason']}"
    summary += "."
    print(summary, file=sys.stderr)


# ===========================================================================
# Divisional ancestor walk (--divisionals)
# ===========================================================================

_DIVISIONAL_MAX_DEPTH = 10


def _walk_divisional_ancestors(
    start_app_no: str,
    start_pub_no: str | None,
    max_depth: int = _DIVISIONAL_MAX_DEPTH,
) -> list[dict]:
    """
    Walk upward through the EP divisional parent chain via OPS register biblio.

    For each node, calls `ops_client.extract_divisional_parent(biblio)` to find
    the populated `<reg:parent-doc>` entry (if any) and recurses to that parent.
    Downstream children / sibling divisionals are intentionally NOT followed —
    `--divisionals` is a "fetch upstream context" feature.

    Returns ancestors in closest-first order: [direct parent, grandparent, ...].
    Empty list when the start patent has no parent (root or no
    `<reg:related-documents>` entries).

    Bounded by ``max_depth`` and a visited-set cycle guard. Long-form OPS app
    numbers are auto-normalised by `resolver.resolve()`.

    Each entry::

        {"app_no": "<short-form 8-digit>", "pub_no": "<7-digit or None>",
         "relationship": "divisional parent", "depth": <int>,
         "via": "<child app_no whose biblio surfaced this parent>"}
    """
    related: list[dict] = []
    visited: set[str] = {start_app_no}
    # Each queue item: (app_no, pub_no, depth)
    queue: list[tuple[str, str | None, int]] = [(start_app_no, start_pub_no, 0)]

    while queue:
        cur_app, cur_pub, depth = queue.pop(0)
        if depth >= max_depth:
            print(f"  [divisionals] depth cap ({max_depth}) reached at EP{cur_app} — "
                  f"not expanding further", file=sys.stderr)
            continue
        if not cur_pub:
            # Without a publication we cannot fetch register biblio. Pending
            # applications without pubs are reachable as endpoints but cannot
            # surface further family members.
            continue

        biblio = ops_client.get_register_biblio(f"EP{cur_pub}")
        if not biblio:
            print(f"  [divisionals] depth {depth}: register biblio not "
                  f"available for EP{cur_pub} — cannot expand from this node",
                  file=sys.stderr)
            continue

        # Upward only: follow `extract_divisional_parent` (populated
        # <reg:parent-doc>) and ignore the downward / sibling entries
        # surfaced by `extract_divisional_children`. `--divisionals` is a
        # "fetch upstream context" feature — children of the input are not
        # downloaded.
        candidates: list[tuple[dict, str]] = []
        parent = ops_client.extract_divisional_parent(biblio)
        if parent:
            candidates.append((parent, "divisional parent"))

        for cand, label in candidates:
            resolve_input = (f"EP{cand['pub_doc_number']}" if cand.get("pub_doc_number")
                             else f"EP{cand['app_doc_number']}")
            try:
                cand_app, cand_pub = resolver.resolve(resolve_input)
            except Exception as exc:
                print(f"  [divisionals] resolve({resolve_input}) failed: {exc} — "
                      f"skipping this family member", file=sys.stderr)
                continue
            if not cand_app or cand_app in visited:
                continue
            visited.add(cand_app)
            related.append({
                "app_no":       cand_app,
                "pub_no":       cand_pub,
                "relationship": label,
                "depth":        depth + 1,
                "via":          cur_app,
            })
            queue.append((cand_app, cand_pub, depth + 1))
            print(f"  [divisionals] found {label} EP{cand_app}"
                  + (f" (publication EP{cand_pub})" if cand_pub else "")
                  + f" via EP{cur_app}", file=sys.stderr)

    return related


def _list_pdfs(folder: str) -> list[str]:
    """List *.pdf basenames currently in a folder (sorted)."""
    try:
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(".pdf"))
    except FileNotFoundError:
        return []


def _process_divisionals(
    start_app_no: str,
    start_pub_no: str | None,
    session: RegisterSession,
    root: str,
) -> list[dict]:
    """
    Walk the EP divisional parent chain upward and download bundles for every
    ancestor into a sibling folder under ``root``.

    Each ancestor lands in <root>/EP{ancestor_app_no}/ with its own manifest.
    Returns ordered list of ancestor-entry dicts (closest-first) for
    related.json. Per-ancestor failures are caught and surfaced as `error`
    fields on the entry.
    """
    print("\nWalking divisional parent chain ...", file=sys.stderr)
    family = _walk_divisional_ancestors(start_app_no, start_pub_no)
    if not family:
        # Already-logged in the walker; just confirm we won't write any sibling folders.
        return []

    print(f"  [divisionals] {len(family)} ancestor(s) found — downloading bundles",
          file=sys.stderr)

    entries: list[dict] = []
    for idx, rel in enumerate(family, start=1):
        rel_app  = rel["app_no"]
        rel_pub  = rel["pub_no"]
        depth    = rel["depth"]
        label    = rel["relationship"]
        rel_folder = os.path.join(root, f"EP{rel_app}")
        entry: dict = {
            "index":        idx,
            "relationship": f"{label} (depth {depth})",
            "via":          rel.get("via"),
            "app_no":       rel_app,
            "pub_no":       rel_pub,
            "folder_name":  f"EP{rel_app}",
            "folder":       os.path.abspath(rel_folder),
            "downloaded":   [],
            "failures":     [],
        }
        print(f"\n--- related {idx}/{len(family)}: EP{rel_app} ({label}, "
              f"depth {depth}) ---", file=sys.stderr)
        try:
            os.makedirs(rel_folder, exist_ok=True)
            rel_meta, rel_docs = _fetch_meta_and_doclist(rel_app, rel_pub, session)

            if not rel_docs:
                entry["error"] = "no documents available from KOPD or EPO Register"
                entry["downloaded"] = _list_pdfs(rel_folder)
                entries.append(entry)
                continue

            four = ep_bundles.build_four_bundles(rel_docs)
            to_download = [b for b in four if b["type"] in ep_config.DIVISIONAL_BUNDLES]
            manifest = _load_manifest(rel_folder)
            state, failures = _download_bundles(
                to_download, session, rel_app, rel_folder, manifest,
                pub_no=rel_meta.get("publication_number"),
                kind_code=rel_meta.get("kind_code"),
                grant_date=rel_meta.get("grant_date"),
            )
            _finalize_manifest(rel_folder, rel_app, state, failures)

            entry["downloaded"] = _list_pdfs(rel_folder)
            entry["failures"]   = [{"filename": f["filename"], "reason": f["reason"]}
                                   for f in failures]
            entry["status"]     = rel_meta.get("status")
            entry["patent_number"] = rel_meta.get("patent_number")
        except Exception as exc:
            print(f"  [divisionals] related EP{rel_app} failed: {exc}", file=sys.stderr)
            entry["error"]      = str(exc)
            entry["downloaded"] = _list_pdfs(rel_folder)
        entries.append(entry)

    return entries


def _save_related(
    output_dir: str, app_no: str, meta: dict,
    divisional_entries: list[dict],
) -> None:
    """
    Write <output_dir>/related.json with source metadata + ordered divisional list.

    `source.downloaded` reflects all *.pdf files currently in output_dir so a
    re-run on an already-populated folder shows the full file set. Folder paths
    are absolute. Matches the schema produced by `bundles_api._save_related` on
    the US side.
    """
    payload = {
        "source": {
            "jurisdiction":      "EP",
            "app_no":            app_no,
            "pub_no":            meta.get("publication_number"),
            "kind_code":         meta.get("kind_code"),
            "patent_number":     meta.get("patent_number"),
            "title":             meta.get("title"),
            "status":            meta.get("status"),
            "filing_date":       meta.get("filing_date"),
            "grant_date":        meta.get("grant_date"),
            "saved_at":          datetime.now(timezone.utc).isoformat(),
            "folder":            os.path.abspath(output_dir),
            "downloaded":        _list_pdfs(output_dir),
        },
        "divisionals": divisional_entries,
    }
    path = os.path.join(output_dir, "related.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"  -> related.json written ({len(divisional_entries)} ancestor entries)",
          file=sys.stderr)


# ===========================================================================
# Main
# ===========================================================================

def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch prosecution bundles for an EP (European) application.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("RUN FROM THE COMMAND LINE")[1] if __doc__ else "",
    )
    p.add_argument("number",
                   nargs="+",
                   help="One or more EP application/publication numbers, or WO/PCT publications "
                        "(space-, comma-, or pipe-separated). In bulk mode each patent gets its "
                        "own EP{app_no}/ subfolder inside --output-dir. "
                        "Examples: EP2420929, 10173239, EP3456789A1, WO2015077217.")
    p.add_argument("--separate-bundles", action="store_true",
                   help="One PDF per prosecution round (default: 4-bundle collapse)")
    p.add_argument("--show-extra",   action="store_true",
                   help="Include supporting admin docs (delivery notes, receipts, minutes, oral-proc prep)")
    p.add_argument("--show-intclaim", action="store_true",
                   help="Include intermediate claim docs in round bundles")
    p.add_argument("--download",     action="store_true",
                   help="Download each bundle as a merged PDF to disk")
    p.add_argument("--output-dir",   default=None,
                   help="Directory to save PDFs (default: ./EP{app_number}/)")
    p.add_argument("--base-url",     default="http://localhost:7901",
                   help="Base URL for download_url links (default: http://localhost:7901)")
    p.add_argument("--text",         action="store_true",
                   help="Human-readable text table (default: JSON)")
    p.add_argument("--list-docs",    action="store_true",
                   help="List every document + classification and exit — NO download, no bundling")
    p.add_argument("--divisionals",  action="store_true",
                   help="If the input EP patent is itself a divisional, also download "
                        "bundles for every ancestor in its parent chain (immediate "
                        "parent → ... → root, capped at 10 levels). Each ancestor lands "
                        "in a sibling folder EP{ancestor_app_no}/ under the output root. "
                        "Bundle types per ep/config.py DIVISIONAL_BUNDLES. Writes "
                        "related.json in the main patent's folder. Folder layout: when "
                        "set, main lands in <output-dir>/EP{app_no}/ (root defaults to "
                        "./ep_patents/); without the flag the existing flat layout is "
                        "unchanged. No-op when the patent has no parent.")
    return p


def _process_one_ep_patent(
    input_str: str,
    args: argparse.Namespace,
    parent_output_dir: str | None,
) -> bool:
    """
    Resolve + fetch + (optionally) download one EP patent.

    parent_output_dir: if set, saves to <parent_output_dir>/EP{app_no}/
                       (bulk mode). None → use args.output_dir or default.
    Returns True on success, False on any fatal error.
    """
    try:
        app_no, pub_no, meta, documents, session = _fetch_everything(input_str)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False

    if not documents:
        print("No prosecution documents found in the EPO Register for this application.",
              file=sys.stderr)
        return False

    if args.list_docs:
        _cmd_list_docs(meta, documents)
        return True

    # Folder layout:
    #   * Bulk mode (parent_output_dir set) — main goes into
    #     <parent_output_dir>/EP{app_no}/, unchanged.
    #   * --divisionals — main goes into <root>/EP{app_no}/ so divisional
    #     ancestor siblings can live alongside under the same <root>. Default
    #     root is ./ep_patents/ when --output-dir isn't passed.
    #   * Otherwise — legacy flat layout: PDFs land directly in <output-dir>/
    #     or ./EP{app_no}/.
    if parent_output_dir is not None:
        root       = parent_output_dir
        output_dir = os.path.join(parent_output_dir, f"EP{app_no}")
    elif args.divisionals:
        root       = args.output_dir if args.output_dir is not None else "./ep_patents"
        output_dir = os.path.join(root, f"EP{app_no}")
    else:
        root       = args.output_dir if args.output_dir is not None else "."
        output_dir = args.output_dir if args.output_dir is not None else f"EP{app_no}"

    # ======================================================= SEPARATE-BUNDLES mode
    if args.separate_bundles:
        bundles_list = ep_bundles.build_prosecution_bundles(documents)
        base = args.base_url.rstrip("/")
        flag_qs = (f"?show_extra={str(args.show_extra).lower()}"
                   f"&show_intclaim={str(args.show_intclaim).lower()}")

        result_bundles = []
        for bundle in bundles_list:
            visible = ep_bundles._filter_docs(
                bundle["documents"], show_extra=args.show_extra, show_intclaim=args.show_intclaim
            )
            result_bundles.append({
                "index": bundle["index"],
                "label": bundle["label"],
                "type":  bundle["type"],
                "download_url": f"{base}/ep/bundles/{app_no}/{bundle['index']}/pdf{flag_qs}",
                "documents": visible,
            })

        if not args.text:
            print(json.dumps({**meta, "bundles": result_bundles,
                              "total_rounds": sum(1 for b in bundles_list
                                                   if b["type"] in ("round", "final_round"))},
                             indent=2, default=str))
        else:
            _print_metadata_header(meta)
            total_rounds = sum(1 for b in bundles_list if b["type"] in ("round", "final_round"))
            print(f"\nBundles: {len(result_bundles)}   OA rounds: {total_rounds}\n")
            for bundle, rb in zip(bundles_list, result_bundles):
                print(f"[{rb['index']}] {rb['label']}")
                print(f"    Download: {rb['download_url']}")
                if not rb["documents"]:
                    print("    (no documents visible with current flags)")
                for doc in rb["documents"]:
                    pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                    tier = doc.get("category", "default")
                    tag = ep_config.category_label(tier)
                    print(f"    {doc['date']}  {doc['code']:<8} "
                          f"{doc['doc_type'][:55]:<55}  {pages:>4}{tag}")
                print()

        if args.download:
            os.makedirs(output_dir, exist_ok=True)
            manifest = _load_manifest(output_dir)
            state, failures = _download_separate(
                bundles_list, session, app_no, output_dir, manifest,
                args.show_extra, args.show_intclaim
            )
            _finalize_manifest(output_dir, app_no, state, failures)
        return True

    # ======================================================= DEFAULT: 4-bundle mode
    four = ep_bundles.build_four_bundles(documents)

    if not args.text:
        print(json.dumps({**meta, "bundles": [
            {"filename": b["filename"], "label": b["label"], "type": b["type"],
             "documents": b["documents"]} for b in four
        ]}, indent=2, default=str))
    else:
        _print_metadata_header(meta)
        print(f"\n4-bundle mode  (use --separate-bundles for one PDF per round)\n")
        for b in four:
            print(f"[{b['filename']}]")
            if not b["documents"]:
                print("    (no documents)")
                continue
            for doc in b["documents"]:
                pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                tier  = doc.get("category", "default")
                tag   = ep_config.category_label(tier)
                print(f"    {doc['date']}  {doc['code']:<8} "
                      f"{doc['doc_type'][:55]:<55}  {pages:>4}{tag}")
            print()

    if args.download:
        os.makedirs(output_dir, exist_ok=True)
        manifest = _load_manifest(output_dir)
        to_download = [b for b in four if b["type"] in ep_config.SOURCE_BUNDLES]
        state, failures = _download_bundles(
            to_download, session, app_no, output_dir, manifest,
            pub_no=meta.get("publication_number"),
            kind_code=meta.get("kind_code"),
            grant_date=meta.get("grant_date"),
        )
        _finalize_manifest(output_dir, app_no, state, failures)

        # --divisionals: walk parent chain, download each ancestor into its own
        # sibling folder under <root>, then write related.json in the main folder.
        if args.divisionals:
            divisional_entries = _process_divisionals(app_no, pub_no, session, root)
            _save_related(output_dir, app_no, meta, divisional_entries)

    return True


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)

    # Flatten all tokens — split on commas and pipes so any separator style works
    raw_tokens: list[str] = []
    for token in args.number:
        raw_tokens.extend(re.split(r"[,|]+", token))
    inputs = [t.strip() for t in raw_tokens if t.strip()]

    if len(inputs) == 1:
        ok = _process_one_ep_patent(inputs[0], args, parent_output_dir=None)
        return 0 if ok else 1

    # ------------------------------------------------------------------ Bulk mode
    parent_dir = args.output_dir  # None → each patent defaults to ./EP{app_no}/ in cwd
    n = len(inputs)
    print(f"\nBulk mode: {n} patents", file=sys.stderr)
    if parent_dir:
        print(f"Output root: {parent_dir}/EP{{app_no}}/", file=sys.stderr)
    else:
        print("Output root: ./EP{app_no}/ (per patent)", file=sys.stderr)

    results: list[tuple[str, bool]] = []
    pbar = tqdm(inputs, desc="Patents", unit="patent")
    for inp in pbar:
        pbar.set_postfix_str(inp)
        tqdm.write(f"\n{'='*60}\n[{len(results)+1}/{n}] {inp}\n{'='*60}")
        ok = _process_one_ep_patent(inp, args, parent_output_dir=parent_dir)
        results.append((inp, ok))

    succeeded   = sum(1 for _, ok in results if ok)
    failed_list = [(inp, ok) for inp, ok in results if not ok]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Bulk run complete: {succeeded}/{n} succeeded.", file=sys.stderr)
    if failed_list:
        print("Failed patents:", file=sys.stderr)
        for inp, _ in failed_list:
            print(f"  - {inp}", file=sys.stderr)

    return 0 if not failed_list else 1


if __name__ == "__main__":
    sys.exit(main())

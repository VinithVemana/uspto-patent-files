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

    python bundles_api_ep.py EP2420929 EP2985974 EP3456789B1 --download --output-dir ./bulk
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
from datetime import datetime, timezone

from tqdm import tqdm

from ep import bundles as ep_bundles
from ep import config as ep_config
from ep import ops_client, pdf as ep_pdf, resolver
from ep.register_client import RegisterSession


# ===========================================================================
# Shared helpers (metadata fetch, manifest)
# ===========================================================================

MANIFEST_FILE = "manifest.json"


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

    # Pull OPS biblio for metadata (publication number preferred; if only app,
    # we still try via /search to find the earliest publication)
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
    # Ensure app_number is populated (our truth is what the resolver returned)
    meta["application_number"] = app_no

    # Scrape the register for the actual document list
    print("Fetching document list from EPO Register ...", file=sys.stderr)
    session = RegisterSession()
    documents = session.list_documents(f"EP{app_no}")
    print(f"Found {len(documents)} documents.", file=sys.stderr)

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

def _download_bundles(
    bundles: list[dict], session: RegisterSession, app_no: str, output_dir: str,
    manifest: dict,
) -> tuple[dict, list[dict]]:
    """Download the 4-bundle collapse. Returns (artifacts_state, failures)."""
    state: dict = {}
    failures: list[dict] = []

    for b in bundles:
        if not b["documents"]:
            continue
        filename = f"{b['filename']}.pdf"
        fp       = ep_pdf.doc_fingerprint(b["documents"])
        key      = f"bundle_{b['filename']}"
        needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
        if not needed:
            state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
            print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
            continue

        print(f"  [{filename}] {reason} — downloading {len(b['documents'])} docs", file=sys.stderr)
        bar = tqdm(total=len(b["documents"]), desc=filename, file=sys.stderr, leave=False)

        def cb(doc, _bar=bar):
            _bar.set_postfix_str(f"{doc.get('code','?')} {doc['doc_type'][:40]}")
            _bar.update(1)

        try:
            merged = ep_pdf.merge_bundle_pdfs(
                session, b, app_no,
                show_extra=False, show_intclaim=False,
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

    if parent_output_dir is not None:
        output_dir = os.path.join(parent_output_dir, f"EP{app_no}")
    else:
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
        state, failures = _download_bundles(four, session, app_no, output_dir, manifest)
        _finalize_manifest(output_dir, app_no, state, failures)

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

"""
bundles_api.py — USPTO prosecution-bundle CLI
=============================================

Core logic lives in the `us/` module so the FastAPI server can import it directly.

INPUT FORMATS ACCEPTED
----------------------
All CLI arguments accept any of these formats:

  Application number     16123456          bare digits
  Formatted app number   16/123,456        slashes and commas stripped automatically
  Patent grant number    US10902286        'US' prefix → patent-to-app lookup
  Patent with kind code  US11973593B2      kind code (B2/B1/A1…) stripped automatically
  Bare patent digits     11973593          tries app number first, falls back to patent lookup
                         11973593 --patent  force patent-to-app lookup with --patent flag

RUN FROM THE COMMAND LINE
-------------------------
    python bundles_api.py <number> [options]

    Options:
      --patent            Force input to be treated as a patent grant number
      --text              Human-readable text table (default output is JSON)
      --show-extra        Also include OA support docs, amendments, advisory actions, RCE docs
      --show-intclaim     Also include intermediate CLM docs in round bundles
      --download          Download each bundle as a merged PDF to disk
      --output-dir DIR    Where to save PDFs (default: ./{app_no}/)
      --separate-bundles  One PDF per prosecution round (default: 3-bundle collapse)
      --base-url URL      Base URL for download_url links (default: http://localhost:7901)

WEB SERVER
----------
    uvicorn bundles_server:app --host 0.0.0.0 --port 7901
"""

# Re-export the full public surface so bundles_server.py can keep its
# existing `from bundles_api import (...)` import unchanged.
from us.config import HEADERS, GOOGLE_PATENTS_HEADERS
from us.client import fetch_json, _get_metadata, _get_documents
from us.resolver import (
    resolve_application_number,
    resolve_patent_to_application,
    resolve_publication_to_application,
    _extract_patent_digits,
    _is_publication_number,
)
from us.bundles import (
    build_prosecution_bundles,
    _build_three_bundles,
    _doc_category,
    _filter_docs,
)
from us.pdf import get_patent_pdf_url, _merge_bundle_pdfs, _merge_fwclm_pdf
from us.manifest import (
    MANIFEST_FILE,
    _doc_fingerprint,
    _load_manifest,
    _save_manifest,
    _needs_download,
)

if __name__ == "__main__":
    import argparse
    import json
    import os
    import re
    import sys

    import requests

    parser = argparse.ArgumentParser(
        description="Fetch prosecution bundles for a USPTO application (JSON output by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 3-bundle mode (default): initial_claims + REM-CTNF-... + granted_claims
  python bundles_api.py 16123456
  python bundles_api.py 16123456 --download --output-dir ./pdfs

  # Human-readable text table
  python bundles_api.py 16123456 --text

  # One PDF per prosecution round (original per-round mode)
  python bundles_api.py 16123456 --separate-bundles
  python bundles_api.py 16123456 --separate-bundles --show-extra --show-intclaim
  python bundles_api.py 16123456 --separate-bundles --download --output-dir ./pdfs

  # Custom base URL for download_url links in separate-bundles mode
  python bundles_api.py 16123456 --separate-bundles --base-url https://myserver.example.com
        """,
    )
    parser.add_argument("application_number",
                        help="USPTO application number (e.g. 16123456) or patent grant number "
                             "(e.g. US10902286, US11973593B2, 11973593). "
                             "Formatting like '16/123,456' is accepted.")
    parser.add_argument("--separate-bundles", action="store_true",
                        help="One PDF per prosecution round (default: merge into 3 PDFs)")
    parser.add_argument("--show-extra",       action="store_true",
                        help="Include OA support docs, amendments, advisory, RCE docs")
    parser.add_argument("--show-intclaim",    action="store_true",
                        help="Include intermediate CLM docs in round bundles")
    parser.add_argument("--download",         action="store_true",
                        help="Download each bundle as a merged PDF to disk")
    parser.add_argument("--output-dir",       default=None,
                        help="Directory to save PDFs (default: ./{app_no}/)")
    parser.add_argument("--base-url",         default="http://localhost:7901",
                        help="Base URL for download_url links (default: http://localhost:7901)")
    parser.add_argument("--patent",            action="store_true",
                        help="Force input to be treated as a patent grant number")
    parser.add_argument("--text",             action="store_true",
                        help="Print a human-readable text table instead of JSON")
    args = parser.parse_args()

    # --- Resolve & fetch ---
    print(f"Resolving {args.application_number} ...", file=sys.stderr)
    try:
        app_no = resolve_application_number(args.application_number, force_patent=args.patent)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Application number: {app_no}", file=sys.stderr)

    meta = _get_metadata(app_no)
    if not meta:
        print(f"ERROR: Application '{args.application_number}' not found in USPTO.", file=sys.stderr)
        sys.exit(1)

    bundles = build_prosecution_bundles(app_no)
    if not bundles:
        print("No prosecution documents found.", file=sys.stderr)
        sys.exit(0)

    output_dir      = args.output_dir if args.output_dir is not None else app_no
    manifest        = _load_manifest(output_dir) if args.download else {}
    _artifact_state: dict = {}
    _failures:       list = []

    def _download_patent_pdf() -> tuple[bool, str]:
        patent_no = meta.get("patent_number")
        if not patent_no:
            print("  (no patent number — application not yet granted, skipping patent.pdf)",
                  file=sys.stderr)
            return False, "no patent number"
        filename = f"US{patent_no}.pdf"
        filepath = os.path.join(output_dir, filename)
        print(f"  Fetching full patent PDF for US{patent_no} ...", file=sys.stderr)
        pdf_url = get_patent_pdf_url(patent_no)
        if not pdf_url:
            print(f"  Patent PDF not found on Google Patents for US{patent_no}", file=sys.stderr)
            return False, "PDF URL not found on Google Patents (may be bot-blocked)"
        try:
            r = requests.get(pdf_url, headers=GOOGLE_PATENTS_HEADERS, timeout=60, stream=True)
            r.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    fh.write(chunk)
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  Saved {filename} ({size_kb:,} KB)  <-  {pdf_url}", file=sys.stderr)
            return True, ""
        except Exception as exc:
            print(f"  Failed to download patent PDF: {exc}", file=sys.stderr)
            return False, f"download error: {exc}"

    def _download_index_of_claims() -> tuple[bool, str]:
        filepath = os.path.join(output_dir, "Index_of_claims.pdf")
        print("  Fetching Index of Claims (FWCLM) ...", file=sys.stderr)
        try:
            pdf = _merge_fwclm_pdf(app_no)
            with open(filepath, "wb") as fh:
                fh.write(pdf.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  Saved Index_of_claims.pdf ({size_kb:,} KB)", file=sys.stderr)
            return True, ""
        except ValueError as exc:
            print(f"  Index of Claims not available: {exc}", file=sys.stderr)
            return False, f"merge failed: {exc}"
        except Exception as exc:
            print(f"  Index of Claims write failed: {exc}", file=sys.stderr)
            return False, f"write error: {exc}"

    def _record_skip(key: str, filename: str, fp: str) -> None:
        _artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}

    def _record_success(key: str, filename: str, fp: str) -> None:
        _artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": True}

    def _record_failure(key: str, filename: str, reason: str) -> None:
        _failures.append({"key": key, "filename": filename, "reason": reason})

    def _download_sep_bundle_smart(bundle: dict, safe: str) -> None:
        key      = f"sep_bundle_{bundle['index']}"
        filename = f"{safe}.pdf"
        docs     = _filter_docs(bundle["documents"], bundle["type"],
                                args.show_extra, args.show_intclaim)
        fp       = _doc_fingerprint(docs) if docs else ""
        needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
        if not needed:
            _record_skip(key, filename, fp)
            print(f"    [{filename}] up-to-date — skipped", file=sys.stderr)
            return
        print(f"    [{filename}] {reason} — downloading", file=sys.stderr)
        filepath = os.path.join(output_dir, filename)
        try:
            pdf = _merge_bundle_pdfs(bundle, args.show_extra, args.show_intclaim)
            with open(filepath, "wb") as fh:
                fh.write(pdf.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"    -> Saved ({size_kb:,} KB)", file=sys.stderr)
            _record_success(key, filename, fp)
        except ValueError as exc:
            print(f"    -> Failed: {exc}", file=sys.stderr)
            _record_failure(key, filename, f"merge failed: {exc}")
        except Exception as exc:
            print(f"    -> Failed: {exc}", file=sys.stderr)
            _record_failure(key, filename, f"write error: {exc}")

    def _download_patent_pdf_smart() -> None:
        patent_no = meta.get("patent_number")
        if not patent_no:
            print("  (no patent number — not yet granted, skipping patent.pdf)", file=sys.stderr)
            return
        filename       = f"US{patent_no}.pdf"
        needed, reason = _needs_download("patent_pdf", filename, patent_no, manifest, output_dir)
        if not needed:
            _record_skip("patent_pdf", filename, patent_no)
            print(f"  [US{patent_no}.pdf] up-to-date — skipped", file=sys.stderr)
            return
        print(f"  [US{patent_no}.pdf] {reason} — downloading", file=sys.stderr)
        ok, fail_reason = _download_patent_pdf()
        if ok:
            _record_success("patent_pdf", filename, patent_no)
        else:
            _record_failure("patent_pdf", filename, fail_reason)

    def _download_index_smart() -> None:
        fwclm_docs = [d for d in _get_documents(app_no) if d["code"] == "FWCLM"]
        if not fwclm_docs:
            return
        fp             = _doc_fingerprint(fwclm_docs)
        needed, reason = _needs_download("index_of_claims", "Index_of_claims.pdf",
                                         fp, manifest, output_dir)
        if not needed:
            _record_skip("index_of_claims", "Index_of_claims.pdf", fp)
            print("  [Index_of_claims.pdf] up-to-date — skipped", file=sys.stderr)
            return
        print(f"  [Index_of_claims.pdf] {reason} — downloading", file=sys.stderr)
        ok, fail_reason = _download_index_of_claims()
        if ok:
            _record_success("index_of_claims", "Index_of_claims.pdf", fp)
        else:
            _record_failure("index_of_claims", "Index_of_claims.pdf", fail_reason)

    def _finalize_manifest() -> None:
        if not _artifact_state and not _failures:
            return
        downloaded = sum(1 for v in _artifact_state.values() if v.get("needed"))
        skipped    = sum(1 for v in _artifact_state.values() if not v.get("needed"))
        failed     = len(_failures)
        _save_manifest(output_dir, app_no, _artifact_state, _failures)
        summary = f"\nSummary: {downloaded} downloaded, {skipped} skipped"
        if failed:
            summary += f", {failed} failed"
            for f in _failures:
                summary += f"\n  - {f['filename']}: {f['reason']}"
        summary += "."
        print(summary, file=sys.stderr)

    # ================================================================== SEPARATE-BUNDLES mode
    if args.separate_bundles:
        base         = args.base_url.rstrip("/")
        flag_qs      = f"?show_extra={str(args.show_extra).lower()}&show_intclaim={str(args.show_intclaim).lower()}"
        total_rounds = sum(1 for b in bundles if b["type"] in ("round", "final_round"))

        result_bundles = []
        for bundle in bundles:
            bundle_type  = bundle["type"]
            visible_docs = _filter_docs(bundle["documents"], bundle_type, args.show_extra, args.show_intclaim)
            result_bundles.append({
                "index":        bundle["index"],
                "label":        bundle["label"],
                "type":         bundle_type,
                "download_url": f"{base}/bundles/{app_no}/{bundle['index']}/pdf{flag_qs}",
                "documents":    visible_docs,
            })

        if not args.text:
            output = {**meta, "total_rounds": total_rounds, "bundles": result_bundles}
            print(json.dumps(output, indent=2))
            if args.download:
                os.makedirs(output_dir, exist_ok=True)
                for bundle, rb in zip(bundles, result_bundles):
                    if not rb["documents"]:
                        continue
                    safe = re.sub(r"[^\w\s\-]", "", bundle["label"]).strip().replace(" ", "_")
                    _download_sep_bundle_smart(bundle, safe)
                _download_patent_pdf_smart()
                _download_index_smart()
                _finalize_manifest()
            sys.exit(0)

        # Text output
        print("=" * 64)
        print(f"Title:         {meta['title']}")
        print(f"Status:        {meta['status']}")
        print(f"Filing date:   {meta['filing_date']}")
        print(f"Patent no.:    {meta.get('patent_number') or 'N/A'}")
        print(f"Grant date:    {meta.get('grant_date') or 'N/A'}")
        print(f"Pub no.:       {meta.get('pub_number') or 'N/A'}")
        print(f"Examiner:      {meta['examiner']}  (AU {meta['art_unit']})")
        print(f"Inventors:     {', '.join(i['name'] for i in meta['inventors']) or 'N/A'}")
        print(f"Applicants:    {', '.join(meta['applicants']) or 'N/A'}")
        print("=" * 64)
        print(f"\nBundles: {len(result_bundles)}   OA rounds: {total_rounds}\n")

        if args.download:
            os.makedirs(output_dir, exist_ok=True)

        for bundle, rb in zip(bundles, result_bundles):
            bundle_type = bundle["type"]
            print(f"[{rb['index']}] {rb['label']}")
            print(f"    Download: {rb['download_url']}")
            if not rb["documents"]:
                print("    (no documents visible with current flags)")
            else:
                for doc in rb["documents"]:
                    pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                    cat   = _doc_category(doc["code"], bundle_type)
                    tag   = {"default": "", "intclaim": " [int-claim]", "extra": " [extra]"}.get(cat, "")
                    print(f"    {doc['date'][:10]}  {doc['code']:<12}  "
                          f"{doc['desc'][:48]:<48}  {pages:>4}{tag}")
            if args.download and rb["documents"]:
                safe = re.sub(r"[^\w\s\-]", "", bundle["label"]).strip().replace(" ", "_")
                _download_sep_bundle_smart(bundle, safe)
            print()
        if args.download:
            _download_patent_pdf_smart()
            _download_index_smart()
            _finalize_manifest()
        sys.exit(0)

    # ================================================================== DEFAULT: 3-bundle mode
    three = _build_three_bundles(bundles)

    def _download_three(b: dict) -> None:
        filepath = os.path.join(output_dir, f"{b['filename']}.pdf")
        print(f"    -> Downloading to {filepath} ...")
        try:
            pdf = _merge_bundle_pdfs({"type": b["type"], "documents": b["documents"]},
                                     show_extra=False, show_intclaim=False)
            with open(filepath, "wb") as fh:
                fh.write(pdf.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"    -> Saved ({size_kb:,} KB)")
        except ValueError as exc:
            print(f"    -> Failed: {exc}")

    def _download_three_smart(b: dict) -> None:
        key            = f"bundle_{b['type']}"
        filename       = f"{b['filename']}.pdf"
        fp             = _doc_fingerprint(b["documents"]) if b["documents"] else ""
        needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
        _artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": needed}
        if not needed:
            print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
            return
        print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
        _download_three(b)

    if not args.text:
        output = {
            **meta,
            "bundles": [
                {"filename": b["filename"], "label": b["label"],
                 "type": b["type"], "documents": b["documents"]}
                for b in three
            ],
        }
        print(json.dumps(output, indent=2))
        if args.download:
            os.makedirs(output_dir, exist_ok=True)
            for b in three:
                if b["documents"]:
                    _download_three_smart(b)
            _download_patent_pdf_smart()
            _download_index_smart()
            _finalize_manifest()
        sys.exit(0)

    # Text output
    print("=" * 64)
    print(f"Title:         {meta['title']}")
    print(f"Status:        {meta['status']}")
    print(f"Filing date:   {meta['filing_date']}")
    print(f"Patent no.:    {meta.get('patent_number') or 'N/A'}")
    print(f"Grant date:    {meta.get('grant_date') or 'N/A'}")
    print(f"Pub no.:       {meta.get('pub_number') or 'N/A'}")
    print(f"Examiner:      {meta['examiner']}  (AU {meta['art_unit']})")
    print(f"Inventors:     {', '.join(i['name'] for i in meta['inventors']) or 'N/A'}")
    print(f"Applicants:    {', '.join(meta['applicants']) or 'N/A'}")
    print("=" * 64)
    print(f"\n3-bundle mode  (use --separate-bundles for one PDF per round)\n")

    if args.download:
        os.makedirs(output_dir, exist_ok=True)

    for b in three:
        print(f"[{b['filename']}]")
        if not b["documents"]:
            print("    (no documents)")
        else:
            for doc in b["documents"]:
                pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                print(f"    {doc['date'][:10]}  {doc['code']:<12}  "
                      f"{doc['desc'][:48]:<48}  {pages:>4}")
        if args.download and b["documents"]:
            _download_three_smart(b)
        print()

    if args.download:
        _download_patent_pdf_smart()
        _download_index_smart()
        _finalize_manifest()

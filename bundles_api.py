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

BULK MODE
---------
Pass multiple patents as space-, comma-, or pipe-separated values.
Each patent gets its own subfolder inside --output-dir.

    python bundles_api.py US10897328B2 US10912060B2 US10952166B2 --download --output-dir ./bulk
    python bundles_api.py "US10897328B2,US10912060B2,US10952166B2" --download --output-dir ./bulk
    python bundles_api.py "US10897328B2|US10912060B2|US10952166B2" --download --output-dir ./bulk

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
      --continuations     Also download bundles for every CON/CIP ancestor.
                          Files land in the input patent's folder suffixed
                          _parent_NN (newest filing date first). Types per
                          us/config.py CONTINUATION_BUNDLES.
      --disclaimers       OCR every DISQ decision; for each approved Terminal
                          Disclaimer download bundles for every cited prior
                          patent. Files land in the input patent's folder
                          suffixed _TD_NN (descending order). Types per
                          DISCLAIMER_BUNDLES. Requires pdftoppm + tesseract on PATH.
      --base-url URL      Base URL for download_url links (default: http://localhost:7901)

      python bundles_api.py 18221238 --download  --continuations
      python bundles_api.py 12141042 --download  --disclaimers

GRANTED CLAIMS SOURCE
---------------------
Every Granted_claims*.pdf — main bundle, _TD_NN (--disclaimers), and
_parent_NN (--continuations) — is built from Dolcera Solr
(`srch11.dolcera.net:12080`, collection `alexandria-101123`) when reachable.
Solr mirrors the issued grant verbatim, avoiding examiner amendments that
may appear in the latest USPTO CLM document, and works for legacy patents
whose USPTO granted bundle is empty. Falls back to the USPTO bundle merge
automatically when srch11 is unreachable, has no match, or returns
malformed claims. See us/srch11.py.

WEB SERVER
----------
    uvicorn bundles_server:app --host 0.0.0.0 --port 7901
"""

# Re-export the full public surface so bundles_server.py can keep its
# existing `from bundles_api import (...)` import unchanged.
from us.config import HEADERS, GOOGLE_PATENTS_HEADERS, CONTINUATION_BUNDLES, DISCLAIMER_BUNDLES
from us.client import fetch_json, _get_metadata, _get_documents, _get_continuity
from us.resolver import (
    resolve_application_number,
    resolve_patent_to_application,
    resolve_publication_to_application,
    _extract_patent_digits,
    _is_publication_number,
)
from us.disclaimer import get_disq_decisions
from us.bundles import (
    build_prosecution_bundles,
    _build_three_bundles,
    _doc_category,
    _filter_docs,
)
from us.pdf import get_patent_pdf_url, _merge_bundle_pdfs, _merge_fwclm_pdf
from us import srch11
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

    # ------------------------------------------------------------------
    # Granted-claims source-picking helpers — shared across the main flow,
    # `_process_continuations`, and `_process_disclaimers`.
    # ------------------------------------------------------------------
    def _granted_claims_planned_fingerprint(b: dict, patent_no: str | None) -> str:
        """
        Predict which source `_build_granted_claims_pdf` will use, so the
        caller's up-to-date check uses the matching fingerprint format and
        re-runs detect source swaps.
        """
        if patent_no and srch11.is_reachable():
            return f"srch11:{patent_no}"
        return _doc_fingerprint(b["documents"]) if b["documents"] else ""

    def _build_granted_claims_pdf(
        b: dict, patent_no: str | None, grant_date: str | None,
        filepath: str, log_label: str = "Granted_claims",
    ) -> tuple[bool, str, str, str]:
        """
        Source policy for any Granted_claims PDF (main / TD / continuation):
          1. srch11 (Dolcera Solr) when reachable and patent_no is known
          2. USPTO `_merge_bundle_pdfs` when bundle has documents
          3. failure if neither is available

        Returns ``(ok, source, fingerprint, reason)``. ``source`` is one of
        ``"srch11"`` / ``"uspto"``; ``fingerprint`` matches the source so
        re-runs detect swaps; ``reason`` explains failures (or "ok").
        """
        print(f"  [{log_label}] resolving source: "
              f"patent_no={patent_no!r}, docs_count={len(b['documents'])}",
              file=sys.stderr)

        if patent_no and srch11.is_reachable():
            buf, srch_reason = srch11.build_granted_claims_pdf(
                patent_no, grant_date
            )
            if buf is not None:
                try:
                    with open(filepath, "wb") as fh:
                        fh.write(buf.getvalue())
                    size_kb = os.path.getsize(filepath) // 1024
                    print(f"  [{log_label}] -> Saved from srch11 ({size_kb:,} KB)",
                          file=sys.stderr)
                    return True, "srch11", f"srch11:{patent_no}", "ok"
                except OSError as exc:
                    print(f"  [{log_label}] srch11 write failed: {exc} "
                          f"— falling back to USPTO", file=sys.stderr)
            else:
                print(f"  [{log_label}] srch11 path unavailable "
                      f"({srch_reason}) — falling back to USPTO",
                      file=sys.stderr)

        if b["documents"]:
            try:
                pdf = _merge_bundle_pdfs(
                    {"type": b["type"], "documents": b["documents"]},
                    show_extra=False, show_intclaim=False,
                )
                with open(filepath, "wb") as fh:
                    fh.write(pdf.getvalue())
                size_kb = os.path.getsize(filepath) // 1024
                print(f"  [{log_label}] -> Saved from USPTO merge "
                      f"({size_kb:,} KB)", file=sys.stderr)
                return True, "uspto", _doc_fingerprint(b["documents"]), "ok"
            except (ValueError, OSError) as exc:
                return False, "", "", f"USPTO merge failed: {exc}"

        return False, "", "", "no source available (srch11 unavailable, USPTO bundle empty)"

    # ------------------------------------------------------------------
    def _process_one_patent(input_str, args, parent_output_dir=None):
        """
        Resolve + fetch + (optionally) download one patent.

        parent_output_dir: if set, saves to <parent_output_dir>/US{patent_no}/
                           (bulk mode). None → use args.output_dir or default.
        Returns True on success, False on any fatal error.
        """
        print(f"Resolving {input_str} ...", file=sys.stderr)
        try:
            app_no = resolve_application_number(input_str, force_patent=args.patent)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return False
        print(f"Application number: {app_no}", file=sys.stderr)

        meta = _get_metadata(app_no)
        if not meta:
            print(f"ERROR: Application '{input_str}' not found in USPTO.", file=sys.stderr)
            return False

        bundles = build_prosecution_bundles(app_no)
        if not bundles:
            print("No prosecution documents found.", file=sys.stderr)
            return False

        patent_no_meta = meta.get("patent_number")
        default_subdir = f"US{patent_no_meta}" if patent_no_meta else app_no

        if parent_output_dir is not None:
            output_dir = os.path.join(parent_output_dir, default_subdir)
        elif args.output_dir is not None:
            output_dir = args.output_dir
        else:
            output_dir = default_subdir

        manifest        = _load_manifest(output_dir) if args.download else {}
        _artifact_state: dict = {}
        _failures:       list = []

        def _download_patent_pdf() -> tuple[bool, str]:
            patent_no = meta.get("patent_number")
            if not patent_no:
                print("  (no patent number — application not yet granted, skipping patent.pdf)",
                      file=sys.stderr)
                return False, "no patent number"
            filename = "Granted_document.pdf"
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
            filename       = "Granted_document.pdf"
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

        # ============================================================== SEPARATE-BUNDLES mode
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
                    if args.continuations:
                        _process_continuations(app_no, output_dir, manifest, _artifact_state, _failures)
                    if args.disclaimers:
                        _process_disclaimers(app_no, output_dir, manifest, _artifact_state, _failures)
                    _finalize_manifest()
                return True

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
                if args.continuations:
                    _process_continuations(app_no, output_dir, manifest, _artifact_state, _failures)
                if args.disclaimers:
                    _process_disclaimers(app_no, output_dir, manifest, _artifact_state, _failures)
                _finalize_manifest()
            return True

        # ============================================================== DEFAULT: 3-bundle mode
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
            key      = f"bundle_{b['type']}"
            filename = f"{b['filename']}.pdf"
            patent_no_local = meta.get("patent_number")
            is_granted_bundle = (
                b["type"] == "granted" and b["filename"] == "Granted_claims"
            )

            # Granted_claims has its own source-picking helper that prefers
            # Dolcera Solr (srch11) and falls back to USPTO _merge_bundle_pdfs.
            # All other bundles use the simple USPTO-only path.
            if is_granted_bundle:
                fp = _granted_claims_planned_fingerprint(b, patent_no_local)
                needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                if not needed:
                    _artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    return
                print(f"  [{filename}] {reason} — downloading", file=sys.stderr)

                filepath = os.path.join(output_dir, filename)
                ok, _src, real_fp, build_reason = _build_granted_claims_pdf(
                    b, patent_no_local, meta.get("grant_date"), filepath,
                    log_label="Granted_claims",
                )
                if ok:
                    _artifact_state[key] = {
                        "filename": filename, "fingerprint": real_fp, "needed": True
                    }
                else:
                    print(f"    -> Failed: {build_reason}", file=sys.stderr)
                    _failures.append({"key": key, "filename": filename, "reason": build_reason})
                return

            # Non-granted bundles: existing USPTO-only path.
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
                    is_granted_with_patent = (
                        b["type"] == "granted"
                        and b["filename"] == "Granted_claims"
                        and meta.get("patent_number")
                    )
                    if b["documents"] or is_granted_with_patent:
                        _download_three_smart(b)
                _download_patent_pdf_smart()
                _download_index_smart()
                if args.continuations:
                    _process_continuations(app_no, output_dir, manifest, _artifact_state, _failures)
                if args.disclaimers:
                    _process_disclaimers(app_no, output_dir, manifest, _artifact_state, _failures)
                _finalize_manifest()
            return True

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
            if args.download:
                is_granted_with_patent = (
                    b["type"] == "granted"
                    and b["filename"] == "Granted_claims"
                    and meta.get("patent_number")
                )
                if b["documents"] or is_granted_with_patent:
                    _download_three_smart(b)
            print()

        if args.download:
            _download_patent_pdf_smart()
            _download_index_smart()
            if args.continuations:
                _process_continuations(app_no, output_dir, manifest, _artifact_state, _failures)
            if args.disclaimers:
                _process_disclaimers(app_no, output_dir, manifest, _artifact_state, _failures)
            _finalize_manifest()
        return True

    # ------------------------------------------------------------------
    def _download_index_for(target_app: str, filename: str, output_dir: str) -> tuple[bool, str]:
        """Fetch most-recent FWCLM (Index of Claims) for target_app and save
        to output_dir/filename. Returns (success, reason)."""
        filepath = os.path.join(output_dir, filename)
        print(f"  Fetching Index of Claims for {target_app} ...", file=sys.stderr)
        try:
            pdf = _merge_fwclm_pdf(target_app)
            with open(filepath, "wb") as fh:
                fh.write(pdf.getvalue())
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  Saved {filename} ({size_kb:,} KB)", file=sys.stderr)
            return True, ""
        except ValueError as exc:
            return False, f"merge failed: {exc}"
        except Exception as exc:
            return False, f"write error: {exc}"

    # ------------------------------------------------------------------
    def _download_granted_for(patent_no: str, filename: str, output_dir: str) -> tuple[bool, str]:
        """Fetch the full granted patent PDF from Google Patents and save it
        to output_dir/filename. Returns (success, reason)."""
        if not patent_no:
            return False, "no patent number"
        filepath = os.path.join(output_dir, filename)
        print(f"  Fetching full patent PDF for US{patent_no} ...", file=sys.stderr)
        pdf_url = get_patent_pdf_url(patent_no)
        if not pdf_url:
            return False, "PDF URL not found on Google Patents (may be bot-blocked)"
        try:
            r = requests.get(pdf_url, headers=GOOGLE_PATENTS_HEADERS, timeout=60, stream=True)
            r.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    fh.write(chunk)
            size_kb = os.path.getsize(filepath) // 1024
            print(f"  Saved {filename} ({size_kb:,} KB)", file=sys.stderr)
            return True, ""
        except Exception as exc:
            return False, f"download error: {exc}"

    # ------------------------------------------------------------------
    def _process_continuations(
        app_no: str,
        output_dir: str,
        manifest: dict,
        artifact_state: dict,
        failures: list,
    ) -> None:
        """
        For each continuation/CIP ancestor of app_no, download bundle types
        listed in CONTINUATION_BUNDLES directly into output_dir with filenames
        suffixed _parent_{NN}. Parents sorted by filing_date DESC (newest first).

        Updates artifact_state and failures in place — caller persists the
        single shared manifest via _finalize_manifest().
        """
        parents = _get_continuity(app_no)
        if not parents:
            print("  No continuation parents found.", file=sys.stderr)
            return

        # Newest filing date first. Empty filing_date sorts last.
        parents.sort(key=lambda p: p.get("filing_date") or "0000-00-00", reverse=True)
        width = max(2, len(str(len(parents))))

        _BUNDLE_TYPE = {"initial": "initial", "middle": "round", "granted": "granted"}
        wanted_types = {_BUNDLE_TYPE[k] for k in CONTINUATION_BUNDLES if k in _BUNDLE_TYPE}
        want_granted_doc = "granted_document" in CONTINUATION_BUNDLES
        want_index = "index_of_claims" in CONTINUATION_BUNDLES

        print(f"\n  Continuation parents ({len(parents)}, newest first):", file=sys.stderr)
        for i, p in enumerate(parents, 1):
            print(f"    {i:0{width}d}. {p['relationship']}: {p['app_no']}"
                  f"  filed={p['filing_date'] or 'N/A'}"
                  f"  patent={p['patent_no'] or 'N/A'}  [{p['status']}]",
                  file=sys.stderr)
        print(file=sys.stderr)

        for i, p in enumerate(parents, 1):
            parent_app = p["app_no"]
            if not parent_app:
                continue
            idx = f"{i:0{width}d}"
            print(f"[Continuation parent_{idx}] {p['relationship']}: {parent_app}",
                  file=sys.stderr)

            try:
                parent_bundles = build_prosecution_bundles(parent_app)
                if not parent_bundles:
                    print(f"  No prosecution docs for {parent_app}", file=sys.stderr)
                    continue
            except Exception as exc:
                print(f"  ERROR fetching {parent_app}: {exc}", file=sys.stderr)
                continue

            three = _build_three_bundles(parent_bundles)
            parent_patent_no = p.get("patent_no") or None
            parent_grant_date: str | None = None
            for b in three:
                if b["type"] not in wanted_types:
                    continue
                is_granted_bundle = (
                    b["type"] == "granted" and b["filename"] == "Granted_claims"
                )
                if not b["documents"] and not is_granted_bundle:
                    continue
                if not b["documents"] and not parent_patent_no:
                    continue

                key      = f"cont_{idx}_bundle_{b['type']}"
                filename = f"{b['filename']}_parent_{idx}.pdf"
                filepath = os.path.join(output_dir, filename)

                if is_granted_bundle:
                    fp = _granted_claims_planned_fingerprint(b, parent_patent_no)
                    needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                    if not needed:
                        artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                        print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                        continue
                    print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                    if parent_grant_date is None and parent_patent_no:
                        parent_meta = _get_metadata(parent_app) or {}
                        parent_grant_date = parent_meta.get("grant_date") or ""
                    ok, _src, real_fp, build_reason = _build_granted_claims_pdf(
                        b, parent_patent_no, parent_grant_date or None, filepath,
                        log_label=filename.removesuffix(".pdf"),
                    )
                    if ok:
                        artifact_state[key] = {
                            "filename": filename, "fingerprint": real_fp, "needed": True
                        }
                    else:
                        print(f"  -> Failed: {build_reason}", file=sys.stderr)
                        failures.append({"key": key, "filename": filename, "reason": build_reason})
                    continue

                fp       = _doc_fingerprint(b["documents"])
                needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    continue
                print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                try:
                    pdf = _merge_bundle_pdfs(
                        {"type": b["type"], "documents": b["documents"]},
                        show_extra=False, show_intclaim=False,
                    )
                    with open(filepath, "wb") as fh:
                        fh.write(pdf.getvalue())
                    size_kb = os.path.getsize(filepath) // 1024
                    print(f"  -> Saved {filename} ({size_kb:,} KB)", file=sys.stderr)
                    artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
                except Exception as exc:
                    print(f"  -> Failed: {exc}", file=sys.stderr)
                    failures.append({"key": key, "filename": filename, "reason": str(exc)})

            if want_index:
                key      = f"cont_{idx}_index_of_claims"
                filename = f"Index_of_claims_parent_{idx}.pdf"
                fwclm_docs = [d for d in _get_documents(parent_app) if d["code"] == "FWCLM"]
                if not fwclm_docs:
                    print(f"  [{filename}] no FWCLM docs — skipping", file=sys.stderr)
                else:
                    fp = _doc_fingerprint(fwclm_docs)
                    needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                    if not needed:
                        artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                        print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    else:
                        print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                        ok, fail_reason = _download_index_for(parent_app, filename, output_dir)
                        if ok:
                            artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
                        else:
                            failures.append({"key": key, "filename": filename, "reason": fail_reason})

            if want_granted_doc:
                patent_no = p.get("patent_no")
                key      = f"cont_{idx}_patent_pdf"
                filename = f"Granted_document_parent_{idx}.pdf"
                if not patent_no:
                    print(f"  [{filename}] parent not granted — skipping", file=sys.stderr)
                    continue
                needed, reason = _needs_download(key, filename, patent_no, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": patent_no, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    continue
                print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                ok, fail_reason = _download_granted_for(patent_no, filename, output_dir)
                if ok:
                    artifact_state[key] = {"filename": filename, "fingerprint": patent_no, "needed": True}
                else:
                    failures.append({"key": key, "filename": filename, "reason": fail_reason})

    # ------------------------------------------------------------------
    def _process_disclaimers(
        app_no: str,
        output_dir: str,
        manifest: dict,
        artifact_state: dict,
        failures: list,
    ) -> None:
        """
        For each APPROVED Terminal Disclaimer (DISQ) on app_no, download bundle
        types in DISCLAIMER_BUNDLES for every cited prior patent directly into
        output_dir with filenames suffixed _TD_{NN}.

        Cited patents are reversed (descending) from the order they appeared
        across DISQ decisions. Updates artifact_state/failures in place — caller
        persists the single shared manifest.
        """
        decisions = get_disq_decisions(app_no)
        if not decisions:
            print("  No DISQ documents found.", file=sys.stderr)
            return

        # Collect approved cited patents (de-dup, preserve order across DISQs).
        cited: list[str] = []
        approved_count = 0
        for dec in decisions:
            if dec["approved"] is not True:
                print(f"  [DISQ {dec['date'][:10]}] approved={dec['approved']} — skipping",
                      file=sys.stderr)
                continue
            approved_count += 1
            for pn in dec["patents"]:
                if pn not in cited:
                    cited.append(pn)

        if not cited:
            print("  No approved Terminal Disclaimers with cited patents.", file=sys.stderr)
            return

        # Descending order — reverse the natural collection order.
        cited.reverse()

        _BUNDLE_TYPE = {"initial": "initial", "middle": "round", "granted": "granted"}
        wanted_types = {_BUNDLE_TYPE[k] for k in DISCLAIMER_BUNDLES if k in _BUNDLE_TYPE}
        want_granted_doc = "granted_document" in DISCLAIMER_BUNDLES
        want_index = "index_of_claims" in DISCLAIMER_BUNDLES

        width = max(2, len(str(len(cited))))
        print(f"\n  Terminal Disclaimer cited patents ({len(cited)} from "
              f"{approved_count} approved DISQ, descending):", file=sys.stderr)
        for i, pn in enumerate(cited, 1):
            print(f"    TD_{i:0{width}d}. US{pn}", file=sys.stderr)
        print(file=sys.stderr)

        for i, patent_no in enumerate(cited, 1):
            idx = f"{i:0{width}d}"
            print(f"[Disclaimer TD_{idx}] US{patent_no}", file=sys.stderr)
            try:
                td_app = resolve_patent_to_application(patent_no)
            except Exception as exc:
                print(f"  resolve failed: {exc}", file=sys.stderr)
                continue
            if not td_app:
                print(f"  Could not resolve US{patent_no} — skipping", file=sys.stderr)
                continue

            try:
                td_bundles = build_prosecution_bundles(td_app)
                if not td_bundles:
                    print(f"  No prosecution docs for {td_app}", file=sys.stderr)
                    continue
            except Exception as exc:
                print(f"  ERROR fetching {td_app}: {exc}", file=sys.stderr)
                continue

            three = _build_three_bundles(td_bundles)
            td_grant_date: str | None = None
            for b in three:
                if b["type"] not in wanted_types:
                    continue
                is_granted_bundle = (
                    b["type"] == "granted" and b["filename"] == "Granted_claims"
                )
                # Skip non-granted bundles when their docs are empty (existing
                # behavior). Granted bundle goes through the source-picking
                # helper, which can build from srch11 even with empty USPTO docs.
                if not b["documents"] and not is_granted_bundle:
                    continue
                if not b["documents"] and not patent_no:
                    continue

                key      = f"td_{idx}_bundle_{b['type']}"
                filename = f"{b['filename']}_TD_{idx}.pdf"
                filepath = os.path.join(output_dir, filename)

                if is_granted_bundle:
                    fp = _granted_claims_planned_fingerprint(b, patent_no)
                    needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                    if not needed:
                        artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                        print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                        continue
                    print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                    if td_grant_date is None:
                        td_meta = _get_metadata(td_app) or {}
                        td_grant_date = td_meta.get("grant_date") or ""
                    ok, _src, real_fp, build_reason = _build_granted_claims_pdf(
                        b, patent_no, td_grant_date or None, filepath,
                        log_label=filename.removesuffix(".pdf"),
                    )
                    if ok:
                        artifact_state[key] = {
                            "filename": filename, "fingerprint": real_fp, "needed": True
                        }
                    else:
                        print(f"  -> Failed: {build_reason}", file=sys.stderr)
                        failures.append({"key": key, "filename": filename, "reason": build_reason})
                    continue

                fp       = _doc_fingerprint(b["documents"])
                needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    continue
                print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                try:
                    pdf = _merge_bundle_pdfs(
                        {"type": b["type"], "documents": b["documents"]},
                        show_extra=False, show_intclaim=False,
                    )
                    with open(filepath, "wb") as fh:
                        fh.write(pdf.getvalue())
                    size_kb = os.path.getsize(filepath) // 1024
                    print(f"  -> Saved {filename} ({size_kb:,} KB)", file=sys.stderr)
                    artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
                except Exception as exc:
                    print(f"  -> Failed: {exc}", file=sys.stderr)
                    failures.append({"key": key, "filename": filename, "reason": str(exc)})

            if want_index:
                key      = f"td_{idx}_index_of_claims"
                filename = f"Index_of_claims_TD_{idx}.pdf"
                fwclm_docs = [d for d in _get_documents(td_app) if d["code"] == "FWCLM"]
                if not fwclm_docs:
                    print(f"  [{filename}] no FWCLM docs — skipping", file=sys.stderr)
                else:
                    fp = _doc_fingerprint(fwclm_docs)
                    needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                    if not needed:
                        artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                        print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    else:
                        print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                        ok, fail_reason = _download_index_for(td_app, filename, output_dir)
                        if ok:
                            artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
                        else:
                            failures.append({"key": key, "filename": filename, "reason": fail_reason})

            if want_granted_doc:
                key      = f"td_{idx}_patent_pdf"
                filename = f"Granted_document_TD_{idx}.pdf"
                needed, reason = _needs_download(key, filename, patent_no, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": patent_no, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    continue
                print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                ok, fail_reason = _download_granted_for(patent_no, filename, output_dir)
                if ok:
                    artifact_state[key] = {"filename": filename, "fingerprint": patent_no, "needed": True}
                else:
                    failures.append({"key": key, "filename": filename, "reason": fail_reason})

    # ------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Fetch prosecution bundles for a USPTO application (JSON output by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single patent
  python bundles_api.py 16123456
  python bundles_api.py 16123456 --download --output-dir ./pdfs

  # Bulk download — space, comma, or pipe separated; each gets its own subfolder
  python bundles_api.py US10897328B2 US10912060B2 US10952166B2 --download --output-dir ./bulk
  python bundles_api.py "US10897328B2,US10912060B2,US10952166B2" --download --output-dir ./bulk
  python bundles_api.py "US10897328B2|US10912060B2|US10952166B2" --download --output-dir ./bulk

  # Human-readable text table
  python bundles_api.py 16123456 --text

  # One PDF per prosecution round (original per-round mode)
  python bundles_api.py 16123456 --separate-bundles
  python bundles_api.py 16123456 --separate-bundles --show-extra --show-intclaim
  python bundles_api.py 16123456 --separate-bundles --download --output-dir ./pdfs

  # Also download REM-CTNF-NOA + Granted_document (per us/config.py CONTINUATION_BUNDLES)
  # for every continuation/CIP ancestor. Files saved into the input patent's own folder
  # with names like REM-CTNF-NOA_parent_01.pdf, Granted_document_parent_01.pdf.
  # Parents listed newest filing date first.
  python bundles_api.py 18221238 --download --output-dir ./pdfs --continuations

  # Also OCR every DISQ (Terminal Disclaimer decision); for each approved one,
  # pull REM-CTNF-NOA + Granted_document (per DISCLAIMER_BUNDLES) for every cited
  # prior patent. Files saved into the input patent's own folder with names like
  # REM-CTNF-NOA_TD_01.pdf, Granted_document_TD_01.pdf (descending order).
  python bundles_api.py 12141042 --download --output-dir ./pdfs --disclaimers
        """,
    )
    parser.add_argument(
        "application_number",
        nargs="+",
        help="One or more USPTO application/patent numbers (space-, comma-, or pipe-separated). "
             "In bulk mode each patent gets its own subfolder inside --output-dir.",
    )
    parser.add_argument("--separate-bundles", action="store_true",
                        help="One PDF per prosecution round (default: merge into 3 PDFs)")
    parser.add_argument("--show-extra",       action="store_true",
                        help="Include OA support docs, amendments, advisory, RCE docs")
    parser.add_argument("--show-intclaim",    action="store_true",
                        help="Include intermediate CLM docs in round bundles")
    parser.add_argument("--download",         action="store_true",
                        help="Download each bundle as a merged PDF to disk")
    parser.add_argument("--output-dir",       default=None,
                        help="Directory to save PDFs. Single patent: saves here directly. "
                             "Bulk mode: each patent gets its own US{no}/ subfolder inside this dir.")
    parser.add_argument("--base-url",         default="http://localhost:7901",
                        help="Base URL for download_url links (default: http://localhost:7901)")
    parser.add_argument("--patent",            action="store_true",
                        help="Force input to be treated as a patent grant number")
    parser.add_argument("--text",             action="store_true",
                        help="Print a human-readable text table instead of JSON")
    parser.add_argument("--continuations",    action="store_true",
                        help="Also download bundles for every continuation/CIP ancestor "
                             "(types in us/config.py CONTINUATION_BUNDLES). Files land in "
                             "the same folder as the input patent's bundles, suffixed "
                             "_parent_NN. Parents listed newest filing date first.")
    parser.add_argument("--disclaimers",      action="store_true",
                        help="Also OCR every Terminal Disclaimer (DISQ) decision; for each "
                             "approved disclaimer download bundles for every cited prior "
                             "patent (types in us/config.py DISCLAIMER_BUNDLES). Files land "
                             "in the same folder as the input patent's bundles, suffixed "
                             "_TD_NN (descending order). Requires pdftoppm and tesseract on PATH.")
    args = parser.parse_args()

    # Flatten all tokens — split on commas and pipes so any separator style works
    raw_tokens: list[str] = []
    for token in args.application_number:
        raw_tokens.extend(re.split(r"[,|]+", token))
    inputs = [t.strip() for t in raw_tokens if t.strip()]

    if len(inputs) == 1:
        ok = _process_one_patent(inputs[0], args, parent_output_dir=None)
        sys.exit(0 if ok else 1)

    # ------------------------------------------------------------------ Bulk mode
    from tqdm import tqdm

    parent_dir = args.output_dir  # None → each patent defaults to ./US{no}/ in cwd
    n = len(inputs)
    print(f"\nBulk mode: {n} patents", file=sys.stderr)
    if parent_dir:
        print(f"Output root: {parent_dir}/US{{patent_no}}/", file=sys.stderr)
    else:
        print("Output root: ./US{patent_no}/ (per patent)", file=sys.stderr)

    results: list[tuple[str, bool]] = []
    pbar = tqdm(inputs, desc="Patents", unit="patent")
    for inp in pbar:
        pbar.set_postfix_str(inp)
        tqdm.write(f"\n{'='*60}\n[{len(results)+1}/{n}] {inp}\n{'='*60}")
        ok = _process_one_patent(inp, args, parent_output_dir=parent_dir)
        results.append((inp, ok))

    succeeded  = sum(1 for _, ok in results if ok)
    failed_list = [(inp, ok) for inp, ok in results if not ok]

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Bulk run complete: {succeeded}/{n} succeeded.", file=sys.stderr)
    if failed_list:
        print("Failed patents:", file=sys.stderr)
        for inp, _ in failed_list:
            print(f"  - {inp}", file=sys.stderr)

    sys.exit(0 if not failed_list else 1)

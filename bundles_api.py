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

OUTPUT LAYOUT
-------------
All patent folders live as siblings under a single root directory:

  <root>/                              ← --output-dir, default `./us_patents/`
    US12167405/                        ← granted patent → folder name `US{patent_no}`
      US12167405_Initial_claims.pdf    ← every file prefixed `US{patent_no}_`
      US12167405_REM-CTNF-NOA.pdf
      US12167405_Granted_claims.pdf
      US12167405_Index_of_claims.pdf
      US12167405_Granted_document.pdf
      manifest.json                    ← per-folder dedup manifest
      related.json                     ← only on main; lists continuations + TDs
    US{parent_patent_no}/              ← continuation parents are siblings
      US{parent_patent_no}_Initial_claims.pdf
      ...
      manifest.json
    app_15987654/                      ← un-granted parent → folder `app_{app_no}`
      app_15987654_Initial_claims.pdf  ← files prefixed `app_{app_no}_`
      ...
    US{td_patent_no}/                  ← TD-cited patents are siblings
      ...

Re-running for a parent / TD-cited patent later reuses its own folder + manifest →
no duplicate downloads.

BULK MODE
---------
Pass multiple patents as space-, comma-, or pipe-separated values.
Each input patent gets its own folder under <root>; their continuations / TDs
are siblings inside the same root.

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
      --output-dir DIR    Root directory for all patent folders (default: ./us_patents/).
                          Every patent — main, continuations, TDs — gets a sibling
                          subfolder here.
      --separate-bundles  One PDF per prosecution round (default: 3-bundle collapse)
      --continuations     Also download bundles for every CON/CIP ancestor.
                          Each parent gets its own sibling folder under <root>
                          named US{parent_patent_no}/ (or app_{app_no}/ if not
                          granted). Order recorded in main folder's related.json.
                          Types per us/config.py CONTINUATION_BUNDLES.
      --disclaimers       OCR every DISQ decision; for each approved Terminal
                          Disclaimer download bundles for every cited prior
                          patent. Each cited patent gets its own sibling folder
                          under <root>. Order recorded in main folder's
                          related.json (descending). Types per DISCLAIMER_BUNDLES.
                          Requires pdftoppm + tesseract on PATH.
      --legacy-parents    For --continuations / --disclaimers parents that have
                          no USPTO file-wrapper docs (typically pre-2001 apps),
                          still attempt Granted_claims via srch11 and
                          Granted_document via Google Patents when the parent
                          has a patent number. initial / middle /
                          index_of_claims are skipped (no USPTO docs to merge).
      --base-url URL      Base URL for download_url links (default: http://localhost:7901)

      python bundles_api.py US12167405 --download  --continuations
      python bundles_api.py 12141042 --download  --disclaimers
      python bundles_api.py US8332478B2 --download  --continuations --legacy-parents  # CIP/CON parents pre-2001
      python bundles_api.py US8332478B2 --download  --disclaimers --legacy-parents    # TD-cited patents pre-2001

GRANTED CLAIMS SOURCE
---------------------
Every Granted_claims*.pdf — main bundle plus every continuation / TD sibling —
is built from Dolcera Solr
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
    # Folder + filename naming helpers
    # ------------------------------------------------------------------
    def _id_for(patent_no: str | None, app_no: str) -> tuple[str, str]:
        """
        Return ``(folder_name, file_prefix)`` for an application.

        Granted patents → ``("US{patent_no}", "US{patent_no}_")``
        Otherwise       → ``("app_{app_no}",  "app_{app_no}_")``

        Use this everywhere a patent's per-folder identity is needed so the
        scheme stays consistent across main / continuation / TD downloads.
        """
        if patent_no:
            return f"US{patent_no}", f"US{patent_no}_"
        return f"app_{app_no}", f"app_{app_no}_"

    def _resolve_root(args) -> str:
        """
        Resolve the output root that holds every per-patent sibling folder.

        --output-dir DIR if set; else default ``./us_patents/``.
        """
        return args.output_dir if args.output_dir is not None else "us_patents"

    def _save_related(
        output_dir: str,
        app_no: str,
        patent_no: str | None,
        continuations: list,
        disclaimers: list,
    ) -> None:
        """
        Persist `related.json` in the main patent's folder.

        Records the ordered list of continuation parents and TD-cited patents
        (with their sibling folder paths) discovered during the run. Order
        matches the legacy `_parent_NN` / `_TD_NN` numbering.
        """
        from datetime import datetime
        path = os.path.join(output_dir, "related.json")
        payload = {
            "app_no":        app_no,
            "patent_no":     patent_no,
            "saved_at":      datetime.utcnow().isoformat(),
            "continuations": continuations,
            "disclaimers":   disclaimers,
        }
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"  Saved related.json ({len(continuations)} continuations, "
              f"{len(disclaimers)} disclaimers)", file=sys.stderr)

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
        folder_name, prefix = _id_for(patent_no_meta, app_no)

        # Output root holds every per-patent sibling folder. parent_output_dir
        # is set by bulk mode; otherwise --output-dir DIR or default us_patents/.
        root = parent_output_dir if parent_output_dir is not None else _resolve_root(args)
        output_dir = os.path.join(root, folder_name)

        # Manifest is loaded inline only by --separate-bundles, which keeps the
        # legacy per-round artifact bookkeeping. The 3-bundle main flow
        # delegates entirely to _download_app_artifacts (own manifest inside).
        manifest        = _load_manifest(output_dir) if (args.download and args.separate_bundles) else {}
        _artifact_state: dict = {}
        _failures:       list = []

        def _run_related(main_dir: str) -> None:
            """Run continuation / disclaimer sweeps and persist related.json
            in the main patent's folder (only if there's anything to record)."""
            cont_list = (
                _process_continuations(app_no, root, main_dir, args.legacy_parents)
                if args.continuations else []
            )
            disq_list = (
                _process_disclaimers(app_no, root, main_dir, args.legacy_parents)
                if args.disclaimers else []
            )
            if cont_list or disq_list:
                _save_related(main_dir, app_no, patent_no_meta, cont_list, disq_list)

        # ---- separate-bundles inline helpers (filename prefix applied) ----
        def _download_patent_pdf() -> tuple[bool, str]:
            patent_no = meta.get("patent_number")
            if not patent_no:
                print("  (no patent number — application not yet granted, skipping patent.pdf)",
                      file=sys.stderr)
                return False, "no patent number"
            filename = f"{prefix}Granted_document.pdf"
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
            filename = f"{prefix}Index_of_claims.pdf"
            filepath = os.path.join(output_dir, filename)
            print("  Fetching Index of Claims (FWCLM) ...", file=sys.stderr)
            try:
                pdf = _merge_fwclm_pdf(app_no)
                with open(filepath, "wb") as fh:
                    fh.write(pdf.getvalue())
                size_kb = os.path.getsize(filepath) // 1024
                print(f"  Saved {filename} ({size_kb:,} KB)", file=sys.stderr)
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
            filename = f"{prefix}{safe}.pdf"
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
            filename       = f"{prefix}Granted_document.pdf"
            needed, reason = _needs_download("patent_pdf", filename, patent_no, manifest, output_dir)
            if not needed:
                _record_skip("patent_pdf", filename, patent_no)
                print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                return
            print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
            ok, fail_reason = _download_patent_pdf()
            if ok:
                _record_success("patent_pdf", filename, patent_no)
            else:
                _record_failure("patent_pdf", filename, fail_reason)

        def _download_index_smart() -> None:
            fwclm_docs = [d for d in _get_documents(app_no) if d["code"] == "FWCLM"]
            if not fwclm_docs:
                return
            filename       = f"{prefix}Index_of_claims.pdf"
            fp             = _doc_fingerprint(fwclm_docs)
            needed, reason = _needs_download("index_of_claims", filename,
                                             fp, manifest, output_dir)
            if not needed:
                _record_skip("index_of_claims", filename, fp)
                print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                return
            print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
            ok, fail_reason = _download_index_of_claims()
            if ok:
                _record_success("index_of_claims", filename, fp)
            else:
                _record_failure("index_of_claims", filename, fail_reason)

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
                    _finalize_manifest()
                    _run_related(output_dir)
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
                _finalize_manifest()
                _run_related(output_dir)
            return True

        # ============================================================== DEFAULT: 3-bundle mode
        three = _build_three_bundles(bundles)

        def _run_main_3bundle() -> None:
            """Download main 3-bundle artifacts via the shared helper."""
            _download_app_artifacts(
                app_no          = app_no,
                output_dir      = output_dir,
                patent_no       = patent_no_meta,
                grant_date      = meta.get("grant_date"),
                bundle_keys     = ["initial", "middle", "granted",
                                   "index_of_claims", "granted_document"],
                file_prefix     = prefix,
                legacy_fallback = False,
                bundles         = bundles,
            )

        if not args.text:
            output = {
                **meta,
                "bundles": [
                    {"filename": f"{prefix}{b['filename']}", "label": b["label"],
                     "type": b["type"], "documents": b["documents"]}
                    for b in three
                ],
            }
            print(json.dumps(output, indent=2))
            if args.download:
                _run_main_3bundle()
                _run_related(output_dir)
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

        for b in three:
            print(f"[{prefix}{b['filename']}]")
            if not b["documents"]:
                print("    (no documents)")
            else:
                for doc in b["documents"]:
                    pages = f"{doc['pages']}p" if doc["pages"] else "?p"
                    print(f"    {doc['date'][:10]}  {doc['code']:<12}  "
                          f"{doc['desc'][:48]:<48}  {pages:>4}")
            print()

        if args.download:
            _run_main_3bundle()
            _run_related(output_dir)
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
    # Per-application download core. Owns its own manifest.json. Reused by
    # the main flow (full bundle set) and by every continuation / TD sibling.
    # ------------------------------------------------------------------
    def _download_app_artifacts(
        app_no: str,
        output_dir: str,
        patent_no: str | None,
        grant_date: str | None,
        bundle_keys: list[str],
        file_prefix: str,
        legacy_fallback: bool = False,
        bundles: list | None = None,
    ) -> dict:
        """
        Download bundle types listed in ``bundle_keys`` for one application
        into ``output_dir``. The folder owns its own ``manifest.json`` so
        re-runs hit per-folder dedup independently.

        ``bundle_keys`` is a subset of
        ``{"initial", "middle", "granted", "index_of_claims", "granted_document"}``.

        ``legacy_fallback=True`` synthesizes an empty granted bundle when the
        USPTO file wrapper has no docs (typically pre-2001 apps) — srch11 and
        Google Patents can still source Granted_claims / Granted_document from
        ``patent_no`` alone.

        Returns ``{"downloaded": [...], "skipped": [...], "failures": [...]}``.
        """
        os.makedirs(output_dir, exist_ok=True)
        manifest = _load_manifest(output_dir)
        artifact_state: dict = {}
        failures:       list = []

        _KEY_TO_TYPE = {"initial": "initial", "middle": "round", "granted": "granted"}
        wanted_types = {_KEY_TO_TYPE[k] for k in bundle_keys if k in _KEY_TO_TYPE}
        want_index   = "index_of_claims"  in bundle_keys
        want_granted_doc = "granted_document" in bundle_keys

        if bundles is None:
            try:
                bundles = build_prosecution_bundles(app_no)
            except Exception as exc:
                print(f"  ERROR fetching bundles for {app_no}: {exc}", file=sys.stderr)
                bundles = []

        if not bundles:
            if not (legacy_fallback and patent_no):
                print(f"  No prosecution docs for {app_no}", file=sys.stderr)
                return {"downloaded": [], "skipped": [], "failures": []}
            print(f"  No prosecution docs for {app_no} — legacy fallback "
                  f"(srch11 + Google Patents via patent_no={patent_no})",
                  file=sys.stderr)
            three = (
                [{"type": "granted", "filename": "Granted_claims",
                  "label": "", "documents": []}]
                if "granted" in wanted_types else []
            )
        else:
            three = _build_three_bundles(bundles)

        # initial / middle / granted
        for b in three:
            if b["type"] not in wanted_types:
                continue
            is_granted = (b["type"] == "granted" and b["filename"] == "Granted_claims")
            if not b["documents"] and not is_granted:
                continue
            if not b["documents"] and not patent_no:
                # Granted bundle with no USPTO docs and no patent_no — srch11 cannot help.
                continue

            key      = f"bundle_{b['type']}"
            filename = f"{file_prefix}{b['filename']}.pdf"
            filepath = os.path.join(output_dir, filename)

            if is_granted:
                fp = _granted_claims_planned_fingerprint(b, patent_no)
                needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                    continue
                print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                ok, _src, real_fp, build_reason = _build_granted_claims_pdf(
                    b, patent_no, grant_date or None, filepath,
                    log_label=filename.removesuffix(".pdf"),
                )
                if ok:
                    artifact_state[key] = {"filename": filename, "fingerprint": real_fp, "needed": True}
                else:
                    print(f"  -> Failed: {build_reason}", file=sys.stderr)
                    failures.append({"key": key, "filename": filename, "reason": build_reason})
                continue

            fp = _doc_fingerprint(b["documents"])
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

        # index_of_claims
        if want_index:
            key      = "index_of_claims"
            filename = f"{file_prefix}Index_of_claims.pdf"
            try:
                fwclm_docs = [d for d in _get_documents(app_no) if d["code"] == "FWCLM"]
            except Exception as exc:
                print(f"  [{filename}] FWCLM lookup failed: {exc} — skipping", file=sys.stderr)
                fwclm_docs = []
            if not fwclm_docs:
                if bundles:
                    print(f"  [{filename}] no FWCLM docs — skipping", file=sys.stderr)
            else:
                fp = _doc_fingerprint(fwclm_docs)
                needed, reason = _needs_download(key, filename, fp, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                else:
                    print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                    ok, fail_reason = _download_index_for(app_no, filename, output_dir)
                    if ok:
                        artifact_state[key] = {"filename": filename, "fingerprint": fp, "needed": True}
                    else:
                        failures.append({"key": key, "filename": filename, "reason": fail_reason})

        # granted_document
        if want_granted_doc:
            key      = "patent_pdf"
            filename = f"{file_prefix}Granted_document.pdf"
            if not patent_no:
                print(f"  [{filename}] not granted — skipping", file=sys.stderr)
            else:
                needed, reason = _needs_download(key, filename, patent_no, manifest, output_dir)
                if not needed:
                    artifact_state[key] = {"filename": filename, "fingerprint": patent_no, "needed": False}
                    print(f"  [{filename}] up-to-date — skipped", file=sys.stderr)
                else:
                    print(f"  [{filename}] {reason} — downloading", file=sys.stderr)
                    ok, fail_reason = _download_granted_for(patent_no, filename, output_dir)
                    if ok:
                        artifact_state[key] = {"filename": filename, "fingerprint": patent_no, "needed": True}
                    else:
                        failures.append({"key": key, "filename": filename, "reason": fail_reason})

        if artifact_state or failures:
            _save_manifest(output_dir, app_no, artifact_state, failures)

        downloaded = sum(1 for v in artifact_state.values() if v.get("needed"))
        skipped    = sum(1 for v in artifact_state.values() if not v.get("needed"))
        if artifact_state or failures:
            summary = f"  Folder summary: {downloaded} downloaded, {skipped} skipped"
            if failures:
                summary += f", {len(failures)} failed"
            print(summary, file=sys.stderr)

        return {
            "downloaded": [v["filename"] for v in artifact_state.values() if v.get("needed")],
            "skipped":    [v["filename"] for v in artifact_state.values() if not v.get("needed")],
            "failures":   failures,
        }

    # ------------------------------------------------------------------
    def _process_continuations(
        app_no: str,
        root: str,
        main_output_dir: str,
        legacy_parents: bool = False,
    ) -> list:
        """
        For each continuation/CIP ancestor of ``app_no``, download bundle types
        listed in ``CONTINUATION_BUNDLES`` into a sibling folder under ``root``.

        Each parent gets its own folder + manifest:
          <root>/US{parent_patent_no}/                 (granted)
          <root>/app_{parent_app_no}/                  (un-granted)

        Parents sorted by filing_date DESC (newest first). Returns an ordered
        list of related-entry dicts to be persisted in the main folder's
        ``related.json`` (one entry per parent that had at least an attempt).
        """
        try:
            parents = _get_continuity(app_no)
        except Exception as exc:
            print(f"  Continuity lookup failed: {exc}", file=sys.stderr)
            return []

        if not parents:
            print("  No continuation parents found.", file=sys.stderr)
            return []

        parents.sort(key=lambda p: p.get("filing_date") or "0000-00-00", reverse=True)
        width = max(2, len(str(len(parents))))

        print(f"\n  Continuation parents ({len(parents)}, newest first):", file=sys.stderr)
        for i, p in enumerate(parents, 1):
            print(f"    {i:0{width}d}. {p['relationship']}: {p['app_no']}"
                  f"  filed={p['filing_date'] or 'N/A'}"
                  f"  patent={p['patent_no'] or 'N/A'}  [{p['status']}]",
                  file=sys.stderr)
        print(file=sys.stderr)

        results: list = []
        for i, p in enumerate(parents, 1):
            parent_app = p["app_no"]
            if not parent_app:
                continue

            idx              = f"{i:0{width}d}"
            parent_patent_no = p.get("patent_no") or None
            folder_name, prefix = _id_for(parent_patent_no, parent_app)
            parent_dir = os.path.join(root, folder_name)

            print(f"[Continuation {idx}] {p['relationship']}: "
                  f"{parent_app} → {folder_name}/", file=sys.stderr)

            grant_date = ""
            if parent_patent_no:
                try:
                    grant_date = (_get_metadata(parent_app) or {}).get("grant_date") or ""
                except Exception:
                    grant_date = ""

            summary = _download_app_artifacts(
                app_no          = parent_app,
                output_dir      = parent_dir,
                patent_no       = parent_patent_no,
                grant_date      = grant_date or None,
                bundle_keys     = CONTINUATION_BUNDLES,
                file_prefix     = prefix,
                legacy_fallback = legacy_parents,
            )

            # Skip unreachable / no-data parents from related.json so it
            # tracks only folders that actually exist.
            if not summary["downloaded"] and not summary["skipped"] and not summary["failures"]:
                continue

            try:
                folder_rel = os.path.relpath(parent_dir, main_output_dir)
            except ValueError:
                folder_rel = parent_dir

            results.append({
                "index":        i,
                "relationship": p.get("relationship"),
                "app_no":       parent_app,
                "patent_no":    parent_patent_no,
                "filing_date":  p.get("filing_date"),
                "status":       p.get("status"),
                "folder_name":  folder_name,
                "folder":       folder_rel,
                "downloaded":   summary["downloaded"],
                "failures":     summary["failures"],
            })

        return results

    # ------------------------------------------------------------------
    def _process_disclaimers(
        app_no: str,
        root: str,
        main_output_dir: str,
        legacy_parents: bool = False,
    ) -> list:
        """
        For each APPROVED Terminal Disclaimer (DISQ) on ``app_no``, download
        bundle types in ``DISCLAIMER_BUNDLES`` for every cited prior patent
        into a sibling folder under ``root``.

        Each cited patent gets its own folder + manifest:
          <root>/US{td_patent_no}/

        Cited patents are reversed (descending) from the order they appeared
        across DISQ decisions. Returns an ordered list of related-entry dicts
        to be persisted in the main folder's ``related.json``.
        """
        try:
            decisions = get_disq_decisions(app_no)
        except Exception as exc:
            print(f"  DISQ lookup failed: {exc}", file=sys.stderr)
            return []

        if not decisions:
            print("  No DISQ documents found.", file=sys.stderr)
            return []

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
            return []

        # Descending order — reverse the natural collection order.
        cited.reverse()

        width = max(2, len(str(len(cited))))
        print(f"\n  Terminal Disclaimer cited patents ({len(cited)} from "
              f"{approved_count} approved DISQ, descending):", file=sys.stderr)
        for i, pn in enumerate(cited, 1):
            print(f"    TD_{i:0{width}d}. US{pn}", file=sys.stderr)
        print(file=sys.stderr)

        results: list = []
        for i, patent_no in enumerate(cited, 1):
            idx = f"{i:0{width}d}"
            print(f"[Disclaimer {idx}] US{patent_no}", file=sys.stderr)
            try:
                td_app = resolve_patent_to_application(patent_no)
            except Exception as exc:
                print(f"  resolve failed: {exc}", file=sys.stderr)
                continue
            if not td_app:
                print(f"  Could not resolve US{patent_no} — skipping", file=sys.stderr)
                continue

            folder_name, prefix = _id_for(patent_no, td_app)
            td_dir = os.path.join(root, folder_name)
            print(f"  → {folder_name}/", file=sys.stderr)

            grant_date = ""
            try:
                grant_date = (_get_metadata(td_app) or {}).get("grant_date") or ""
            except Exception:
                grant_date = ""

            summary = _download_app_artifacts(
                app_no          = td_app,
                output_dir      = td_dir,
                patent_no       = patent_no,
                grant_date      = grant_date or None,
                bundle_keys     = DISCLAIMER_BUNDLES,
                file_prefix     = prefix,
                legacy_fallback = legacy_parents,
            )

            if not summary["downloaded"] and not summary["skipped"] and not summary["failures"]:
                continue

            try:
                folder_rel = os.path.relpath(td_dir, main_output_dir)
            except ValueError:
                folder_rel = td_dir

            results.append({
                "index":       i,
                "patent_no":   patent_no,
                "td_app_no":   td_app,
                "folder_name": folder_name,
                "folder":      folder_rel,
                "downloaded":  summary["downloaded"],
                "failures":    summary["failures"],
            })

        return results

    # ------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Fetch prosecution bundles for a USPTO application (JSON output by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single patent → ./us_patents/US{patent_no}/US{patent_no}_{filename}.pdf
  python bundles_api.py 16123456
  python bundles_api.py 16123456 --download

  # Single patent into a custom root → ./pdfs/US{patent_no}/...
  python bundles_api.py 16123456 --download --output-dir ./pdfs

  # Bulk download — space, comma, or pipe separated; each patent + its
  # continuations / TDs all land as siblings inside <root>
  python bundles_api.py US10897328B2 US10912060B2 US10952166B2 --download --output-dir ./bulk
  python bundles_api.py "US10897328B2,US10912060B2,US10952166B2" --download --output-dir ./bulk
  python bundles_api.py "US10897328B2|US10912060B2|US10952166B2" --download --output-dir ./bulk

  # Human-readable text table
  python bundles_api.py 16123456 --text

  # One PDF per prosecution round (original per-round mode)
  python bundles_api.py 16123456 --separate-bundles
  python bundles_api.py 16123456 --separate-bundles --show-extra --show-intclaim
  python bundles_api.py 16123456 --separate-bundles --download --output-dir ./pdfs

  # Also download bundle types in us/config.py CONTINUATION_BUNDLES for every
  # CON/CIP ancestor. Each parent gets its own sibling folder
  # (./us_patents/US{parent_patent_no}/) with files like
  # US{parent_patent_no}_REM-CTNF-NOA.pdf. Order recorded in main folder's
  # related.json (newest filing date first).
  python bundles_api.py 18221238 --download --continuations

  # OCR every DISQ (Terminal Disclaimer decision); for each approved one, pull
  # bundle types in DISCLAIMER_BUNDLES for every cited prior patent. Each cited
  # patent gets its own sibling folder. Order recorded in related.json
  # (descending).
  python bundles_api.py 12141042 --download --disclaimers
        """,
    )
    parser.add_argument(
        "application_number",
        nargs="+",
        help="One or more USPTO application/patent numbers (space-, comma-, or pipe-separated). "
             "Each patent — main, continuations, TDs — lands in its own sibling folder under <root>.",
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
                        help="Root directory holding every per-patent folder "
                             "(default: ./us_patents/). Main, continuation parents, "
                             "and TD-cited patents all land as siblings here, each in "
                             "its own US{patent_no}/ (or app_{app_no}/) subfolder.")
    parser.add_argument("--base-url",         default="http://localhost:7901",
                        help="Base URL for download_url links (default: http://localhost:7901)")
    parser.add_argument("--patent",            action="store_true",
                        help="Force input to be treated as a patent grant number")
    parser.add_argument("--text",             action="store_true",
                        help="Print a human-readable text table instead of JSON")
    parser.add_argument("--continuations",    action="store_true",
                        help="Also download bundles for every continuation/CIP ancestor "
                             "(types in us/config.py CONTINUATION_BUNDLES). Each parent gets "
                             "its own sibling folder under <root>. Order (newest filing date "
                             "first) recorded in main folder's related.json.")
    parser.add_argument("--disclaimers",      action="store_true",
                        help="Also OCR every Terminal Disclaimer (DISQ) decision; for each "
                             "approved disclaimer download bundles for every cited prior "
                             "patent (types in us/config.py DISCLAIMER_BUNDLES). Each cited "
                             "patent gets its own sibling folder under <root>. Order "
                             "(descending) recorded in main folder's related.json. "
                             "Requires pdftoppm and tesseract on PATH.")
    parser.add_argument("--legacy-parents",   action="store_true",
                        help="For --continuations / --disclaimers parents that have no "
                             "USPTO file-wrapper docs (typically pre-2001 apps), still "
                             "attempt Granted_claims via srch11 and Granted_document via "
                             "Google Patents when the parent has a patent number. "
                             "initial / middle / index_of_claims are skipped because there "
                             "are no USPTO docs to merge.")
    args = parser.parse_args()

    # Flatten all tokens — split on commas and pipes so any separator style works
    raw_tokens: list[str] = []
    for token in args.application_number:
        raw_tokens.extend(re.split(r"[,|]+", token))
    inputs = [t.strip() for t in raw_tokens if t.strip()]

    # Single shared root for every patent — main, continuations, TDs all
    # land as siblings here. Default `./us_patents/` if --output-dir omitted.
    root = args.output_dir if args.output_dir is not None else "us_patents"

    if len(inputs) == 1:
        ok = _process_one_patent(inputs[0], args, parent_output_dir=root)
        sys.exit(0 if ok else 1)

    # ------------------------------------------------------------------ Bulk mode
    from tqdm import tqdm

    n = len(inputs)
    print(f"\nBulk mode: {n} patents", file=sys.stderr)
    print(f"Output root: {root}/  (each patent + its continuations/TDs as siblings)",
          file=sys.stderr)

    results: list[tuple[str, bool]] = []
    pbar = tqdm(inputs, desc="Patents", unit="patent")
    for inp in pbar:
        pbar.set_postfix_str(inp)
        tqdm.write(f"\n{'='*60}\n[{len(results)+1}/{n}] {inp}\n{'='*60}")
        ok = _process_one_patent(inp, args, parent_output_dir=root)
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

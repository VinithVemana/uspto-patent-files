# us/ — USPTO Prosecution Bundle Module

Core logic for fetching and organizing USPTO patent prosecution bundles.
CLI entry point is `bundles_api.py` at the project root.

## Module Files

| File | Purpose |
|---|---|
| `config.py` | API key, base URL, `HEADERS`, all document-code sets, `GOOGLE_PATENTS_HEADERS` |
| `client.py` | `fetch_json()` (retry/backoff), `_get_metadata()`, `_get_documents()`, `_get_attorney()`, `_get_continuity()` |
| `resolver.py` | Input normalization + all `resolve_*` functions |
| `bundles.py` | `build_prosecution_bundles()`, `_build_three_bundles()`, `_doc_category()`, `_filter_docs()` |
| `pdf.py` | `get_patent_pdf_url()`, `_merge_bundle_pdfs()`, `_merge_fwclm_pdf()` |
| `manifest.py` | `_doc_fingerprint()`, `_load_manifest()`, `_save_manifest()`, `_needs_download()` |
| `disclaimer.py` | OCR + parse Terminal Disclaimer (`DISQ`) decisions: `get_disq_decisions()`, `parse_disq_text()`, `_ocr_pdf_url()` (shells out to `pdftoppm` + `tesseract`) |
| `srch11.py` | Granted-claims source: queries Dolcera Solr (`srch11.dolcera.net:12080`), parses claim XML via lxml, renders to PDF via reportlab. Used as primary source for every `Granted_claims*.pdf` (main + TD + continuation) with USPTO merge as fallback. |

## Data Flow

```
USPTO API (/meta-data + /documents)
  → resolver.resolve_application_number()    # normalize any input format
  → client._get_metadata()                   # title, status, inventors, CPC, grant info
  → client._get_documents()                  # doc codes, dates, pages, PDF URLs
  → bundles.build_prosecution_bundles()      # organize into Bundle objects
  → bundles._build_three_bundles()           # collapse to 3 logical groups (default)
  → pdf._merge_bundle_pdfs()                 # fetch + merge individual doc PDFs
```

## Document Classification

Codes are bucketed into sets in `config.py`:
- `OA_TRIGGER_CODES`: `CTNF`, `CTFR` — start a new round
- `OA_SUPPORTING_CODES`: `892`, `FWCLM`, `SRFW`, `SRNT`
- `RESPONSE_CODES`: `REM`, `CLM`, `AMND`, `A.1`–`A.3`, `AMSB`, `RCEX`, `RCE`, `AFCP`
- `ADVISORY_CODES`: `CTAV`
- `NOA_CODES`: `NOA`, `ISSUE.NOT`

`bundles._doc_category(code, bundle_type)` assigns visibility tiers:
- `default` — always shown (OA triggers, NOA, initial/granted CLMs, REM)
- `intclaim` — shown only with `--show-intclaim`
- `extra` — shown only with `--show-extra`

## Input Formats Handled by `resolver.py`

| Input | Resolution |
|---|---|
| `16123456` | used as-is |
| `16/123,456` | strips separators → `16123456` |
| `US10902286` | `US` prefix, no pub kind code → patent lookup |
| `US11973593B2` | kind code stripped → patent lookup |
| `US20210367709A1` | pub kind code (A1/A2/A9) → publication lookup |
| `11973593` | try application first; fallback to patent lookup |
| `11973593 --patent` | force patent lookup |

## Output Layout

Every patent — main, continuations, TDs — lives in its own sibling folder under one root directory:

- `<root>` = `--output-dir` if set, else `./us_patents/`.
- Granted patent → folder `US{patent_no}/`, files prefixed `US{patent_no}_`.
- Un-granted application → folder `app_{app_no}/`, files prefixed `app_{app_no}_`.
- Each folder owns its own `manifest.json` for per-folder dedup.

`bundles_api._download_app_artifacts(app_no, output_dir, patent_no, grant_date, bundle_keys, file_prefix, legacy_fallback, bundles=None)` is the single per-application download core, used by:
- the main 3-bundle flow (full bundle set),
- `_process_continuations()` (one call per parent → its own sibling folder),
- `_process_disclaimers()` (one call per TD-cited patent → its own sibling folder).

## Continuation Downloads (`--continuations`)

`client._get_continuity(app_no)` calls `/continuity` and returns parents whose `claimParentageTypeCode` is in `config.CONTINUATION_FOLLOW_CODES` (default `{"CON", "CIP"}`). USPTO returns the full ancestor chain, so one call covers everything.

`bundles_api._process_continuations(app_no, root, main_output_dir, legacy_parents)` sorts parents by `parentApplicationFilingDate` **descending** (newest first) and, for each parent, calls `_download_app_artifacts` into a sibling folder under `<root>` named `US{parent_patent_no}/` (or `app_{parent_app_no}/` when un-granted). Bundle types come from `config.CONTINUATION_BUNDLES` (default `["initial", "middle", "granted", "index_of_claims"]`).

Filename pattern (granted parent): `US{parent_patent_no}_{bundle_filename}.pdf`. Each parent folder has its own `manifest.json`. The function returns an ordered list of related-entry dicts that the caller persists in the main folder's `related.json`.

## Terminal Disclaimer Downloads (`--disclaimers`)

`disclaimer.get_disq_decisions(app_no)` filters `_get_documents()` for `code == "DISQ"`, OCRs each PDF (`pdftoppm` -r 300 + `tesseract`), and returns `[{date, pdf_url, approved, patents}]`. `parse_disq_text()` detects approval via "TDs approved/disapproved" footer or `[x] APPROVED` checkbox, and extracts US patent numbers via the `\d{1,2},\d{3},\d{3}` regex (with bare-digit fallback).

`bundles_api._process_disclaimers(app_no, root, main_output_dir, legacy_parents)` collects approved cited patents (de-duped), **reverses** the list (descending), resolves each via `resolve_patent_to_application()`, and calls `_download_app_artifacts` into a sibling folder `US{td_patent_no}/` under `<root>`. Bundle types come from `config.DISCLAIMER_BUNDLES`. Each cited patent's folder has its own `manifest.json`.

Disapproved decisions are skipped. OCR binaries must be on PATH — install via `brew install poppler tesseract`.

## related.json (main folder only)

`bundles_api._save_related(...)` writes `related.json` to the main patent's folder when `--continuations` and/or `--disclaimers` returns at least one entry. It records the ordered list of sibling folder paths (relative to main) and metadata for every continuation parent and TD-cited patent. Order matches the legacy `_parent_NN` / `_TD_NN` numbering.

## Granted-document & index-of-claims helpers

`bundles_api._download_granted_for(patent_no, filename, output_dir)` — shared helper for fetching the full granted-patent PDF from Google Patents (via `pdf.get_patent_pdf_url`). Returns `(success, reason)`.

`bundles_api._download_index_for(target_app, filename, output_dir)` — shared helper for fetching the most recent FWCLM Index of Claims for an application (via `pdf._merge_fwclm_pdf`). Returns `(success, reason)`.

Both are used by `_process_continuations`, `_process_disclaimers`, and the main flow's smart wrappers.

## Manifest Skip Logic (`manifest.py`)

Downloads are skipped when: file exists on disk AND fingerprint unchanged AND filename unchanged.
Re-downloads when: file missing, not in manifest, filename changed, or documents updated.
Failures are written to `manifest.json` under `failures` key so the next run re-attempts them.

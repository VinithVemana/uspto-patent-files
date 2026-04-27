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

## Continuation Downloads (`--continuations`)

`client._get_continuity(app_no)` calls `/continuity` and returns parents whose `claimParentageTypeCode` is in `config.CONTINUATION_FOLLOW_CODES` (default `{"CON", "CIP"}`). USPTO returns the full ancestor chain, so one call covers everything.

`bundles_api._process_continuations()` iterates parents, builds 3-bundle layout, and downloads only the types listed in `config.CONTINUATION_BUNDLES` (default `["middle"]` = REM-CTNF-NOA). Each parent gets its own `US{patent_no}/` sibling folder under the parent of the main output dir, with its own `manifest.json` for skip logic.

Edit `config.CONTINUATION_BUNDLES` to add `"initial"` or `"granted"` to also pull initial/granted claims for parents.

## Manifest Skip Logic (`manifest.py`)

Downloads are skipped when: file exists on disk AND fingerprint unchanged AND filename unchanged.
Re-downloads when: file missing, not in manifest, filename changed, or documents updated.
Failures are written to `manifest.json` under `failures` key so the next run re-attempts them.

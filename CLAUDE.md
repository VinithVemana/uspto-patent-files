# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Project

**Web server (FastAPI):**
```bash
uvicorn bundles_server:app --host 0.0.0.0 --port 7901
```

**CLI:**
```bash
# By application number
python bundles_api.py 16123456
python bundles_api.py 16/123,456          # formatted — slashes/commas stripped

# By patent grant number
python bundles_api.py US10902286          # US prefix → auto-resolve to app number
python bundles_api.py US11973593B2        # kind code stripped automatically
python bundles_api.py 11973593 --patent   # bare digits, force patent route

# By pre-grant publication number
python bundles_api.py US20210367709A1     # publication kind code A1/A2/A9 → pub lookup

# Key options:
--patent               # force patent-to-app lookup (for bare digit patent numbers)
--separate-bundles     # one PDF per prosecution round (default: 3-bundle collapse)
--show-extra           # include OA support docs, amendments, advisory actions, RCE
--show-intclaim        # include intermediate CLM docs inside round bundles
--download             # write merged PDFs to disk
--output-dir DIR       # default: ./<app_no>/
--base-url URL         # base URL for download links (default: http://localhost:7901)
--text                 # print human-readable table instead of JSON
```

## Dependencies

```
fastapi
uvicorn
requests
PyPDF2
```

No `requirements.txt` exists — install manually.

## Architecture

Two files:
- `bundles_api.py` — standalone CLI + all core logic (USPTO helpers, bundle builders, PDF merge)
- `bundles_server.py` — FastAPI hosting layer; imports from `bundles_api` and exposes HTTP endpoints

### Data Flow

```
USPTO API (/meta-data + /documents)
  → resolve_application_number()     # normalize input; US prefix + A1/A2/A9 → publication→app;
                                     # US prefix + B1/B2 or no kind code → patent→app;
                                     # bare digits → try app first, fallback to patent lookup
  → _get_metadata()                  # title, status, inventors, CPC, grant info
  → _get_documents()                 # doc codes, dates, pages, PDF URLs
  → build_prosecution_bundles()      # organize into Bundle objects
  → [optional] _build_three_bundles()  # collapse to 3 logical groups
  → output: JSON | PDF stream | ZIP | text table
```

### Bundle Types

`build_prosecution_bundles()` produces:
- **Bundle 0** (`initial`): initial CLM documents
- **Bundles 1..N** (`round` / `final_round`): one per Office Action round — each contains the OA, its support docs, and the applicant's response
- **Final bundle** (`granted`): granted claims (NOA + CLM), only if application is granted

`_build_three_bundles()` (default API/CLI mode) collapses to exactly 3:
1. Initial Claims
2. All prosecution rounds merged
3. Granted Claims

### Document Classification

Document codes are bucketed into these sets (see module-level constants):
- `OA_TRIGGER_CODES`: `CTNF`, `CTFR` — start a new round
- `OA_SUPPORTING_CODES`: `892`, `FWCLM`, `SRFW`, `SRNT`
- `RESPONSE_CODES`: `REM`, `CLM`, `AMND`, `A.1`–`A.3`, `AMSB`, `RCEX`, `RCE`, `AFCP`
- `ADVISORY_CODES`: `CTAV`
- `NOA_CODES`: `NOA`, `ISSUE.NOT`

### Document Visibility Tiers

`_doc_category(code, bundle_type)` assigns each doc a tier:
- `default` — always shown (OA triggers, NOA, initial/granted CLMs, REM)
- `intclaim` — shown only with `--show-intclaim` (CLM docs inside round bundles)
- `extra` — shown only with `--show-extra` (OA support, amendments, advisory, RCE)

### API Endpoints

| Endpoint | Description |
|---|---|
| `GET /resolve/{number}` | Resolve patent grant number → application number (e.g. `US10902286` → `16123456`) |
| `GET /bundles/{app_no}` | Metadata + all bundles (JSON) |
| `GET /bundles/{app_no}/{index}/pdf` | Merged PDF for one bundle (streaming) |
| `GET /bundles/{app_no}/all.zip` | ZIP of all bundle PDFs + Index_of_claims.pdf + patent PDF (streaming) |
| `GET /bundles/{app_no}/index-of-claims.pdf` | Merged PDF of all FWCLM (Index of Claims) documents |
| `GET /bundles/{app_no}/patent.pdf` | Full granted patent PDF from Google Patents |

Query params on bundle endpoints: `show_extra` (bool), `show_intclaim` (bool).

All endpoints and the CLI accept a USPTO **application number** (e.g. `16123456`), a **patent grant number** with `US` prefix (e.g. `US10902286`), or a **pre-grant publication number** (e.g. `US20210367709A1`). Grant numbers are resolved via `applicationMetaData.patentNumber`; publication numbers (kind codes A1/A2/A9) via `applicationMetaData.earliestPublicationNumber`.

### Key Utilities

- `fetch_json(url)` — GET with 3-attempt exponential backoff
- `_extract_patent_digits(number)` — strips `US` prefix, kind codes (B2/B1/A1…), commas, slashes → pure digits
- `_is_publication_number(s)` — returns True if kind code is A1/A2/A9 (pre-grant publication)
- `resolve_patent_to_application(patent_digits)` — queries `GET /search?q=applicationMetaData.patentNumber:{n}` to get application number
- `resolve_publication_to_application(pub_number)` — queries `GET /search?q=applicationMetaData.earliestPublicationNumber:{n}` with the full string (e.g. `US20210367709A1` — prefix and kind code must be kept)
- `resolve_application_number(number, force_patent)` — full input resolver; handles all formats (see Input Formats below)
- `get_patent_pdf_url(patent_number)` — scrapes Google Patents CDN URL for granted patent PDF
- `_merge_bundle_pdfs(bundle, ...)` — fetches individual doc PDFs and merges via PyPDF2 with bookmarks
- `_merge_fwclm_pdf(bundles)` — collects all FWCLM docs across all bundles, merges into one PDF (raises ValueError if none found)

### Input Formats

All CLI args and API path params accept:

| Input | Resolution |
|---|---|
| `16123456` | used as-is (application number) |
| `16/123,456` | commas/slashes stripped → `16123456` |
| `US10902286` | `US` prefix, no publication kind code → patent lookup |
| `US11973593B2` | `US` prefix + grant kind code stripped → patent lookup |
| `US20210367709A1` | `US` prefix + publication kind code (A1/A2/A9) → publication lookup |
| `11973593` | try application number; if not found, try patent lookup |
| `11973593 --patent` | force patent lookup (skips application number check) |

## Mistakes Log

**2026-04-20 — Manifest persisted failed downloads as if they succeeded**
DO NOT: populate `_artifact_state[key] = {..., "needed": needed}` *before* calling the underlying download function in the `_download_*_smart` wrappers.
Why: `_download_patent_pdf` / `_download_index_of_claims` / `_merge_bundle_pdfs` catch exceptions internally and return without raising. Recording the artifact up-front meant `_finalize_manifest` wrote a fake "success" entry with the correct fingerprint. Next run's `_needs_download` saw a fingerprint match and silently skipped the missing file forever.
How to apply: Register artifacts in `_artifact_state` **only after** the download function confirms success. Have `_download_patent_pdf` / `_download_index_of_claims` return `tuple[bool, str]` (success, reason). Record failures in a separate `_failures` list that `_save_manifest` persists under a top-level `failures` key — so the user can see what's missing and the next run re-attempts it.

**2026-04-20 — Google Patents bot-blocked a bare `Mozilla/5.0` UA**
DO NOT: send `headers={"User-Agent": "Mozilla/5.0"}` to `patents.google.com`.
Why: That UA string is a known bot fingerprint. Google returns HTTP 503 with the "We're sorry... automated queries" page. `get_patent_pdf_url` treated 503 identically to a real 404, silently returned `None`, and the CLI printed "Patent PDF not found" as if the patent didn't exist on Google Patents at all.
How to apply: Use the shared `GOOGLE_PATENTS_HEADERS` constant (full Chrome UA + `Accept` / `Accept-Language` / `Accept-Encoding`) for every `patents.google.com` and `patentimages.storage.googleapis.com` request. Retry 3× with exponential backoff on 429/5xx and `requests.RequestException`. Log each retry's status code / exception — never swallow with bare `except: continue`, otherwise transient bot-blocks look identical to real 404s.

**2026-04-16 — Server used bundle label instead of filename for PDF naming**
DO NOT: derive download filenames from `bundle["label"]` with `re.sub` (produces `Bundle_0__Initial_Claims.pdf`).
Why: The server was calling `build_prosecution_bundles()` directly and sanitizing the `label` field. This bypassed `_build_three_bundles()` entirely and produced ugly, inconsistent filenames.
How to apply: Always call `_build_three_bundles()` in server endpoints (same as CLI default mode) and use the `filename` field it returns (`initial_claims`, `REM-CTNF-NOA`, `granted_claims`) for `Content-Disposition` headers and ZIP entry names.

**2026-04-16 — all.zip endpoint omitted the patent PDF**
DO NOT: build the zip from prosecution bundles alone without also fetching the full patent PDF.
Why: The `all.zip` endpoint didn't call `_get_metadata()`, so `patent_number` was never available and `get_patent_pdf_url()` was never called. The resulting ZIP was missing `US{patent_no}.pdf`, diverging from the CLI's `--download` behavior which always writes all 3 bundle PDFs **plus** the patent PDF.
How to apply: In `download_all_bundles_zip`, always call `_get_metadata()` first, then after writing the 3 bundle PDFs call `get_patent_pdf_url(patent_no)` and write `US{patent_no}.pdf` into the ZIP — exactly mirroring `_download_patent_pdf()` in the CLI.

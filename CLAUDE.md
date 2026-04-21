# CLAUDE.md

## Running the Project

**Web server (FastAPI):**
```bash
uvicorn bundles_server:app --host 0.0.0.0 --port 7901
```

**USPTO CLI:**
```bash
python bundles_api.py 16123456
python bundles_api.py US10902286          # patent grant number
python bundles_api.py US20210367709A1     # pre-grant publication
python bundles_api.py 16123456 --download --output-dir ./pdfs
python bundles_api.py 16123456 --text
python bundles_api.py US10897328B2 US10912060B2 --download --output-dir ./bulk  # bulk
python bundles_api.py "US10897328B2,US10912060B2" --download --output-dir ./bulk  # comma-sep
python bundles_api.py 16123456 --separate-bundles
```

Key flags: `--patent`, `--separate-bundles`, `--show-extra`, `--show-intclaim`, `--download`, `--output-dir`, `--base-url`, `--text`

**EP CLI:**
```bash
python bundles_api_ep.py EP2985974
python bundles_api_ep.py EP2985974 --text
python bundles_api_ep.py EP2985974 --list-docs
python bundles_api_ep.py EP2985974 --download
```

**EP credentials** — register at developers.epo.org and add to `.env`:
```
EPO_CLIENT_ID=...
EPO_CLIENT_SECRET=...
```

## Dependencies

```
fastapi, uvicorn, requests, PyPDF2, beautifulsoup4, tqdm, python-dotenv
```

No `requirements.txt` — install manually.

## Architecture

```
bundles_api.py       USPTO CLI (imports from us/)
bundles_api_ep.py    EP CLI (imports from ep/)
bundles_server.py    FastAPI server (imports from us/ and ep/)
us/                  USPTO core module — see us/CLAUDE.md
ep/                  EP core module   — see ep/CLAUDE.md
```

### API Endpoints

**USPTO (`/bundles/*`, `/resolve/*`):**

| Endpoint | Description |
|---|---|
| `GET /resolve/{number}` | Resolve any format → application number |
| `GET /bundles/{app_no}` | Metadata + 3 bundles (JSON) |
| `GET /bundles/{app_no}/{index}/pdf` | Merged PDF for one bundle |
| `GET /bundles/{app_no}/all.zip` | ZIP of all bundle PDFs + patent PDF |
| `GET /bundles/{app_no}/index-of-claims.pdf` | Merged FWCLM PDF |
| `GET /bundles/{app_no}/patent.pdf` | Full granted patent PDF (Google Patents) |

**EP (`/ep/bundles/*`, `/ep/resolve/*`):**

| Endpoint | Description |
|---|---|
| `GET /ep/resolve/{number}` | Resolve EP/WO → `{application_number, publication_number}` |
| `GET /ep/bundles/{number}` | Metadata + 4 bundles (JSON) |
| `GET /ep/bundles/{number}/{index}/pdf` | Streamed merged PDF for one bundle (indices 0–3) |
| `GET /ep/bundles/{number}/all.zip` | ZIP of all 4 bundle PDFs |

Query params on bundle endpoints: `show_extra` (bool), `show_intclaim` (bool).

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

**2026-04-21 — Silently accepted truncated EP PDFs when a mid-document page failed**
DO NOT: break out of the page-fetch loop and return whatever pages were already merged when a page raises RuntimeError.
Why: Cloudflare blocks a page mid-document (e.g. page 32 of 34) with a non-PDF response. The "accept partial" logic silently produced a truncated PDF (15 or 31 pages instead of 34) with no error. The user only noticed because the file size was wrong.
How to apply: Retry each failing page individually — wait 30s, force re-warm, retry; wait 90s, retry again; only raise after 3 attempts. Never accept a partial document silently.

**2026-04-21 — Misidentified EPO's own error page as a Cloudflare challenge**
DO NOT: label any "0 documents, no HTML table" response as a "Cloudflare challenge page."
Why: The XHTML 1.0 Transitional doctype is EPO Register's own session/rate-limit error page. Cloudflare challenge pages are HTML5 with "Just a moment…" text. Mislabeling confused the user and the error message.
How to apply: Check response text for EPO-specific markers vs Cloudflare markers before labelling the error. Both cases mean "wait and retry" but the distinction matters for debugging.

**2026-04-16 — all.zip endpoint omitted the patent PDF**
DO NOT: build the zip from prosecution bundles alone without also fetching the full patent PDF.
Why: The `all.zip` endpoint didn't call `_get_metadata()`, so `patent_number` was never available and `get_patent_pdf_url()` was never called. The resulting ZIP was missing `US{patent_no}.pdf`, diverging from the CLI's `--download` behavior which always writes all 3 bundle PDFs **plus** the patent PDF.
How to apply: In `download_all_bundles_zip`, always call `_get_metadata()` first, then after writing the 3 bundle PDFs call `get_patent_pdf_url(patent_no)` and write `US{patent_no}.pdf` into the ZIP — exactly mirroring `_download_patent_pdf()` in the CLI.

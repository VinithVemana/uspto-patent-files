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
python bundles_api.py 18221238 --download --output-dir ./pdfs --continuations  # also pulls every CON/CIP ancestor
python bundles_api.py 12141042 --download --output-dir ./pdfs --disclaimers    # also pulls bundles for every patent cited in an approved DISQ
```

Key flags: `--patent`, `--separate-bundles`, `--show-extra`, `--show-intclaim`, `--download`, `--output-dir`, `--base-url`, `--text`, `--continuations`, `--disclaimers`

### `--continuations`

With `--download`, calls `/continuity` for the input app and downloads bundles for every ancestor whose `claimParentageTypeCode` is in `us/config.py::CONTINUATION_FOLLOW_CODES` (default `{"CON", "CIP"}`). Parents are sorted by `parentApplicationFilingDate` ascending (oldest first). Each parent saves to `{output_dir or "."}/{NN}_US{parent_patent_no}/` where `NN` is the chronological order (zero-padded; falls back to bare app number when not granted).

Bundle types per parent controlled by `us/config.py::CONTINUATION_BUNDLES` — list, edit to taste:
- `"initial"` → `Initial_claims.pdf`
- `"middle"`  → `REM-CTNF-NOA.pdf` (default)
- `"granted"` → `Granted_claims.pdf`

USPTO `/continuity` returns the **full ancestor chain** (not just direct parent), so one call covers the whole tree — no recursion needed.

### `--disclaimers`

With `--download`, OCRs every Terminal Disclaimer review decision (`DISQ` doc code) on the input application. For each **approved** disclaimer, extracts the cited prior US patent numbers and downloads the bundle types in `us/config.py::DISCLAIMER_BUNDLES` (default `["middle"]` → `REM-CTNF-NOA.pdf`) for every cited patent.

Each cited patent saves to `{patent_output_dir}/TD_{NN}_US{patent_no}/` — i.e. **nested inside the input patent's own output folder**, alongside its main bundle PDFs. So a default run with no `--output-dir` produces:

```
US{patent_no}/
  Initial_claims.pdf
  REM-CTNF-NOA.pdf
  Granted_document.pdf
  TD_01_US{cited1}/REM-CTNF-NOA.pdf
  TD_02_US{cited2}/REM-CTNF-NOA.pdf
  ...
```

Manifest skip logic identical to continuations.

DISQ forms are scanned PTOL forms (image-only PDFs), so this requires **OCR**:
- `pdftoppm` (poppler) — converts PDF pages to PNG
- `tesseract` — OCRs the PNGs

Both must be on `PATH` (`brew install poppler tesseract` on macOS). Implementation lives in `us/disclaimer.py`.

Approval detection looks for "TDs approved" / "TDs disapproved" footer text first, then falls back to checkbox-style `[x] APPROVED`. Disapproved decisions are skipped.

**EP CLI:**
```bash
python bundles_api_ep.py EP2985974
python bundles_api_ep.py EP2985974 --text
python bundles_api_ep.py EP2985974 --list-docs
python bundles_api_ep.py EP2985974 --download
python bundles_api_ep.py EP2420929 EP2985974 EP3456789B1 --download --output-dir ./bulk  # bulk
python bundles_api_ep.py "EP2420929,EP2985974" --download --output-dir ./bulk  # comma-sep
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
bjf/                 Barta Jones firm audit — PoA + prosecution status sweep
```

## BJF Audit (`bjf/`)

Sweeps a list of application numbers from `bjf/anorig.txt` (format: `US-18219924` per line, tab-prefixed or plain) and produces `bjf/bjf_results.xlsx` with one row per application.

```bash
python bjf/fetch_bjf_poa.py
```

Columns: `application_number`, `poa_firm`, `poa_firm_address`, `poa_attorneys`, `bjf_match`, `last_oa_code`, `last_oa_date`, `response_filed`, `response_code`, `response_date`, `noa_issued`, `noa_date`, `error`.

- `bjf_match` = `"barta" in poa.lower() AND "jones" in poa.lower()` across firm + every attorney name.
- `last_oa_*` uses `OA_TRIGGER_CODES` (CTNF, CTFR) — the most recent one wins.
- `response_filed` = any `RESPONSE_CODES` doc filed strictly after the last OA date.
- `noa_issued` = any `NOA_CODES` doc present in history.

Reuses `us/client.py::_get_attorney()` (PoA) and `us/client.py::_get_documents()` (timeline). Logs to `bjf/bjf_fetch.log` (DEBUG+) with a tqdm bar on the console.

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

**2026-04-21 — Patent PDF regex missed the kind code in the filename**
DO NOT: hardcode the regex to `US{patent_number}\.pdf` in `get_patent_pdf_url`.
Why: Google Patents stores most grants with the kind code baked into the filename — e.g. `US11516691B2.pdf`, not `US11516691.pdf`. The metadata returns a bare patent number, so the regex never matched and every download printed the misleading "may be bot-blocked" message. The retry machinery was healthy; the regex was wrong.
How to apply: Accept an optional trailing kind code in the filename: `US{patent_number}(?:[A-Z][A-Z0-9]*)?\.pdf`. Test against both modern (`B2`, `B1`, `A1`) and legacy grants where the kind code is absent.

**2026-04-16 — all.zip endpoint omitted the patent PDF**
DO NOT: build the zip from prosecution bundles alone without also fetching the full patent PDF.
Why: The `all.zip` endpoint didn't call `_get_metadata()`, so `patent_number` was never available and `get_patent_pdf_url()` was never called. The resulting ZIP was missing `US{patent_no}.pdf`, diverging from the CLI's `--download` behavior which always writes all 3 bundle PDFs **plus** the patent PDF.
How to apply: In `download_all_bundles_zip`, always call `_get_metadata()` first, then after writing the 3 bundle PDFs call `get_patent_pdf_url(patent_no)` and write `US{patent_no}.pdf` into the ZIP — exactly mirroring `_download_patent_pdf()` in the CLI.

**2026-04-21 — --use-zip feature built on a non-existent EPO endpoint**
DO NOT: add a bulk-ZIP download mode for EPO Register without first verifying the endpoint exists and is accessible from non-browser clients.
Why: The feature assumed a GET endpoint `downloadDocuments?appNumber=...` existed. It doesn't (404). The real mechanism is a form POST to `/download` with selected doc IDs, but Cloudflare blocks that POST from non-browser clients (`403 cf-mitigated: challenge`). The reimplemented fallback (per-doc GETs pre-fetched into memory) downloaded ALL docs including ones not needed for bundles — strictly worse than the regular `--download` path which only fetches docs actually used in bundles.
How to apply: Don't add `--use-zip` or similar bulk-prefetch modes for EPO Register. The regular `--download` per-doc approach is correct. If Cloudflare becomes a problem, the solution is better retry/re-warm logic, not bulk prefetching.

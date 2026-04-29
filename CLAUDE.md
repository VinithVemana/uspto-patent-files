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
python bundles_api.py 16123456 --download                                       # → ./us_patents/US{patent_no}/...
python bundles_api.py 16123456 --download --output-dir ./pdfs                    # → ./pdfs/US{patent_no}/...
python bundles_api.py 16123456 --text
python bundles_api.py US10897328B2 US10912060B2 --download --output-dir ./bulk   # bulk; siblings inside ./bulk/
python bundles_api.py "US10897328B2,US10912060B2" --download --output-dir ./bulk # comma-sep
python bundles_api.py 16123456 --separate-bundles
python bundles_api.py 18221238 --download --continuations                        # parents land as siblings under ./us_patents/
python bundles_api.py 12141042 --download --disclaimers                          # TD-cited patents land as siblings under ./us_patents/
python bundles_api.py US8332478B2 --download --continuations --legacy-parents    # pre-2001 CIP/CON parents → Granted_claims via srch11 + Granted_document via Google Patents
python bundles_api.py 12141042 --download --disclaimers --legacy-parents         # pre-2001 TD-cited patents → same fallback
```

Key flags: `--patent`, `--separate-bundles`, `--show-extra`, `--show-intclaim`, `--download`, `--output-dir`, `--base-url`, `--text`, `--continuations`, `--disclaimers`, `--legacy-parents`

### Output layout

Every patent — main, continuations, TDs — gets its own sibling folder under one root:

```
<root>/                              ← --output-dir, default ./us_patents/
  US12167405/                        ← granted patent → folder `US{patent_no}`
    US12167405_Initial_claims.pdf    ← every file prefixed `US{patent_no}_`
    US12167405_REM-CTNF-NOA.pdf
    US12167405_Granted_claims.pdf
    US12167405_Index_of_claims.pdf
    US12167405_Granted_document.pdf
    manifest.json                    ← per-folder dedup manifest
    related.json                     ← only on main; ordered cont + TD list
  US{parent_patent_no}/              ← continuation parent (sibling)
    US{parent_patent_no}_Initial_claims.pdf
    ...
    manifest.json
  app_15987654/                      ← un-granted parent → folder `app_{app_no}`,
    app_15987654_Initial_claims.pdf  files prefixed `app_{app_no}_`
    ...
  US{td_patent_no}/                  ← TD-cited patent (sibling)
    ...
```

Re-running for a parent / TD-cited patent later reuses its own folder + manifest → zero duplicate downloads.

### `related.json`

Written to the main patent's folder when `--continuations` and/or `--disclaimers` returns at least one entry. Records the ordered list of sibling folders. Order matches the legacy `_parent_NN` / `_TD_NN` numbering (continuations sorted by filing_date DESC; TDs in reversed collection order).

```json
{
  "app_no": "16123456",
  "patent_no": "12167405",
  "saved_at": "2026-04-29T...",
  "continuations": [
    {"index": 1, "relationship": "CON of 16123456",
     "app_no": "15987654", "patent_no": "11876543",
     "filing_date": "2020-03-15", "status": "GRANTED",
     "folder_name": "US11876543", "folder": "../US11876543",
     "downloaded": [...], "failures": [...]}
  ],
  "disclaimers": [
    {"index": 1, "patent_no": "10987654", "td_app_no": "14123456",
     "folder_name": "US10987654", "folder": "../US10987654",
     "downloaded": [...], "failures": [...]}
  ]
}
```

### `--continuations`

With `--download`, calls `/continuity` for the input app and downloads bundles for every ancestor whose `claimParentageTypeCode` is in `us/config.py::CONTINUATION_FOLLOW_CODES` (default `{"CON", "CIP"}`). Parents sorted by `parentApplicationFilingDate` **descending** (newest first). Each parent gets its own sibling folder under `<root>` (`US{parent_patent_no}/` if granted, else `app_{parent_app_no}/`) with its own `manifest.json`.

Bundle types per parent controlled by `us/config.py::CONTINUATION_BUNDLES` (default `["initial", "middle", "granted", "index_of_claims"]`):
- `"initial"`          → `{prefix}Initial_claims.pdf`
- `"middle"`           → `{prefix}REM-CTNF-NOA.pdf`
- `"granted"`          → `{prefix}Granted_claims.pdf`
- `"index_of_claims"`  → `{prefix}Index_of_claims.pdf` (most recent FWCLM)
- `"granted_document"` → `{prefix}Granted_document.pdf` (full Google Patents PDF)

`{prefix}` is `US{parent_patent_no}_` if granted, else `app_{parent_app_no}_`. USPTO `/continuity` returns the **full ancestor chain** (not just direct parent), so one call covers the whole tree — no recursion needed.

### `--legacy-parents`

Optional add-on for `--continuations` and `--disclaimers`. USPTO Open Data Portal returns empty `/meta-data` and `/documents` for **pre-2001 applications** (typically `09XXXXXXX` series and earlier), so without this flag those parents are skipped with `No prosecution docs for {app_no}`.

When `--legacy-parents` is set, every parent that has a `patent_no` in continuity (or every TD-cited patent — TDs always have a patent_no since they're resolved from one) still attempts:

- `"granted"` → `US{patent_no}_Granted_claims.pdf` via **srch11** (Dolcera Solr lookup by patent_no — no USPTO file wrapper needed).
- `"granted_document"` → `US{patent_no}_Granted_document.pdf` via **Google Patents** (also patent_no only).

`"initial"`, `"middle"`, `"index_of_claims"` are skipped because there are no USPTO docs to merge or a most-recent-FWCLM to fetch. Parents without a `patent_no` (rare CIP/CON cases — application abandoned without grant) are still skipped entirely.

Default behavior is unchanged: without the flag, the early `No prosecution docs` skip path runs as before.

### `--disclaimers`

With `--download`, OCRs every Terminal Disclaimer review decision (`DISQ` doc code) on the input application. For each **approved** disclaimer, extracts the cited prior US patent numbers (descending order — reversed from collection order) and downloads the bundle types in `us/config.py::DISCLAIMER_BUNDLES` (default `["initial", "middle", "granted", "index_of_claims"]`) for every cited patent. Each cited patent gets its own sibling folder `US{td_patent_no}/` under `<root>` with its own `manifest.json`.

Bundle keys (same as continuations):
- `"initial"`          → `US{td_patent_no}_Initial_claims.pdf`
- `"middle"`           → `US{td_patent_no}_REM-CTNF-NOA.pdf`
- `"granted"`          → `US{td_patent_no}_Granted_claims.pdf`
- `"index_of_claims"`  → `US{td_patent_no}_Index_of_claims.pdf` (most recent FWCLM)
- `"granted_document"` → `US{td_patent_no}_Granted_document.pdf`

Order recorded in main patent's `related.json` under `disclaimers[]` (descending).

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
fastapi, uvicorn, requests, PyPDF2, beautifulsoup4, tqdm, python-dotenv,
lxml, reportlab
```

No `requirements.txt` — install manually.

## Granted-Claims Source: srch11 → USPTO fallback

Every `Granted_claims*.pdf` produced by `bundles_api.py` — main bundle, every `_TD_NN` (`--disclaimers`), and every `_parent_NN` (`--continuations`) — prefers Dolcera Solr (`srch11.dolcera.net:12080/solr/alexandria-101123`) over the USPTO bundle merge.

Solr query: `pn:"<patent_no>" AND publication_type:"Granted"` with `fl=clm,ucid&rows=1`. Always take `clm[0]` from the response — the `clm` field is a list and the first element is the granted-publication claim XML; later elements are other publication variants and are ignored.

Why prefer Solr: it mirrors the issued grant verbatim. The latest USPTO `CLM` document on the file wrapper can include examiner amendments not present in the published patent. For old patents (pre-AIA, pre-2010s), the granted bundle on USPTO is often empty, so Solr is the **only** source — without it, the granted-claims PDF would not exist at all.

Fallback triggers: srch11 TCP unreachable (2s probe, cached per process), Solr `numFound=0`, malformed XML, parse yields 0 claims, or PDF render error. On fallback, `_merge_bundle_pdfs` runs against the USPTO bundle if it has documents; otherwise the artifact is recorded as a clean `failures` entry in the manifest.

Manifest tagging: when the file came from Solr, the artifact fingerprint is `srch11:{patent_number}` (vs. the 16-hex USPTO doc-fingerprint). Source swaps trigger a re-fetch on the next run automatically.

Logging: every run prints the TCP-probe result, the Solr `numFound`, the `clm` list size, the parsed claim count, and the resolved source decision (`srch11` vs `uspto`) to stderr — so debugging "why is this PDF missing / why is this from USPTO" is a single grep away.

Implementation: `us/srch11.py` (`is_reachable`, `fetch_claims_xml`, `parse_claims`, `render_claims_pdf`, `build_granted_claims_pdf`). Top-level helpers `_granted_claims_planned_fingerprint` and `_build_granted_claims_pdf` in `bundles_api.py` are reused by `_download_three_smart`, `_process_disclaimers`, and `_process_continuations`.

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

# Patent Prosecution Bundles API — USPTO + EP Patent File Wrapper

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.x-3776ab?style=for-the-badge" />
  <img src="https://img.shields.io/badge/FastAPI-Web_Server-009688?style=for-the-badge" />
  <img src="https://img.shields.io/badge/PyPDF2-PDF_Merge-e63946?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Requests-REST_Client-blue?style=for-the-badge" />
  <img src="https://img.shields.io/badge/USPTO-Open_API-2a6ebb?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Regex-Input_Parsing-lightgrey?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Bundles-3_Logical_Groups-orange?style=for-the-badge" />
</p>

<p align="center">
A standalone CLI and FastAPI web server that retrieves USPTO <strong>and EP (European Patent)</strong> prosecution history, classifies documents into logical prosecution bundles, merges them into downloadable PDFs, and serves them via streaming REST endpoints.<br/>
<strong>bundles_api.py</strong> — USPTO CLI &nbsp;|&nbsp; <strong>bundles_api_ep.py</strong> — EP CLI &nbsp;|&nbsp; <strong>bundles_server.py</strong> — FastAPI hosting layer &nbsp;|&nbsp; <strong>us/</strong> — USPTO module (config / client / resolver / bundles / pdf / manifest) &nbsp;|&nbsp; <strong>ep/</strong> — EP module (config / auth / OPS / register / bundles)
</p>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [The USPTO Prosecution Bundle Taxonomy](#-the-uspto-prosecution-bundle-taxonomy)
- [Pipeline](#-pipeline)
- [Step-by-Step Breakdown](#-step-by-step-breakdown)
  - [Step 1: resolve_application_number](#step-1-resolve_application_number) — input normalization + patent-to-app resolution
  - [Step 2: _get_metadata](#step-2-_get_metadata)
  - [Step 3: _get_documents](#step-3-_get_documents)
  - [Step 4: build_prosecution_bundles](#step-4-build_prosecution_bundles)
  - [Step 5: _build_three_bundles](#step-5-_build_three_bundles)
  - [Step 6: _merge_bundle_pdfs](#step-6-_merge_bundle_pdfs)
  - [Step 7: get_patent_pdf_url](#step-7-get_patent_pdf_url)
- [Appendix A — Document Classification Codes](#appendix-a--document-classification-codes)
- [Appendix B — Bundle Types and Visibility Tiers](#appendix-b--bundle-types-and-visibility-tiers)
- [EP (European Patent) Support](#-ep-european-patent-support) — OPS OAuth2 + register.epo.org scraping + PCT-route handling

---

## 🎯 Overview

**bundles_api.py** retrieves a USPTO patent application's complete file wrapper via the USPTO Open API, classifies every document into a prosecution bundle, and exposes those bundles as streamable PDFs through both a FastAPI web server and a command-line interface. In its default mode, the tool collapses all prosecution history into three logical PDFs — *Initial Claims*, *Prosecution History*, and *Granted Claims* — making it easy to snapshot where a patent started, how it was examined, and what was ultimately allowed. The `--separate-bundles` mode preserves the full per-round structure for fine-grained review.

**I/O:**

| Direction | Source / Endpoint | Format | Description |
|:---|:---|:---:|:---|
| Input | USPTO API `/meta-data` | JSON | Application title, status, inventors, CPC, grant info |
| Input | USPTO API `/documents` | JSON | All doc codes, dates, page counts, PDF URLs |
| Input | Google Patents page | HTML | Scraped for granted patent CDN PDF URL |
| Input | CLI positional arg | string | Application number (`16123456`), formatted (`16/123,456`), patent grant number (`US10902286`, `US11973593B2`), pre-grant publication number (`US20210367709A1`), or bare patent digits (`11973593 --patent`) |
| Output | stdout / HTTP | JSON | Metadata + bundle list with `download_url` per bundle |
| Output | HTTP | PDF stream | Merged prosecution bundle PDF |
| Output | HTTP | ZIP stream | All bundle PDFs in one archive |
| Output | disk | PDF file(s) | When `--download` flag is passed |

**Input formats accepted:**

| Input | What happens |
|:---|:---|
| `16123456` | used directly as application number |
| `16/123,456` | commas and slashes stripped → `16123456` |
| `US10902286` | `US` prefix → patent-to-app lookup |
| `US11973593B2` | `US` prefix + grant kind code `B2` stripped → patent-to-app lookup |
| `US20210367709A1` | `US` prefix + publication kind code `A1` → publication-to-app lookup |
| `11973593` | tries as application number first; if not found, falls back to patent lookup |
| `11973593 --patent` | `--patent` flag forces patent-to-app lookup directly |

**Setup:**

```bash
pip install fastapi uvicorn requests PyPDF2 python-dotenv

# Create a .env file with your USPTO API key
echo "USPTO_API_KEY=your_key_here" > .env
```

**CLI usage:**

```bash
# Web server
uvicorn bundles_server:app --host 0.0.0.0 --port 7901

# By application number — 3-bundle mode (default)
python bundles_api.py 16123456
python bundles_api.py 16/123,456               # formatted number — auto-stripped
python bundles_api.py 16123456 --text
python bundles_api.py 16123456 --download --output-dir ./pdfs
python bundles_api.py 16123456 | jq .bundles[].download_url

# Bulk download — space, comma, or pipe separated
# Each patent gets its own US{no}/ subfolder inside --output-dir
python bundles_api.py US10897328B2 US10912060B2 US10952166B2 --download --output-dir ./bulk
python bundles_api.py "US10897328B2,US10912060B2,US10952166B2" --download --output-dir ./bulk
python bundles_api.py "US10897328B2|US10912060B2|US10952166B2" --download --output-dir ./bulk

# By patent grant number — auto-resolved to application number
python bundles_api.py US10902286
python bundles_api.py US11973593B2             # kind code stripped automatically
python bundles_api.py 11973593 --patent        # bare digits, force patent route
python bundles_api.py US10902286 --text
python bundles_api.py US10902286 --download --output-dir ./pdfs

# By pre-grant publication number — auto-resolved to application number
python bundles_api.py US20210367709A1          # A1/A2/A9 kind code → publication lookup
python bundles_api.py US20210367709A1 --text

# One PDF per prosecution round
python bundles_api.py 16123456 --separate-bundles
python bundles_api.py 16123456 --separate-bundles --show-extra --show-intclaim
python bundles_api.py 16123456 --separate-bundles --download

# Custom base URL for download_url links (separate-bundles mode)
python bundles_api.py 16123456 --separate-bundles --base-url https://myserver.example.com

# Re-run on same application — only changed/missing files are re-downloaded
# (a manifest.json in the output dir tracks fingerprints of each artifact)
python bundles_api.py 16123456 --download          # skips files whose docs haven't changed
python bundles_api.py 16123456 --download          # adds Index_of_claims.pdf if it's now missing
```

**Smart re-download (manifest-based caching):**

When `--download` is used, a `manifest.json` is written to the output directory after every run. On subsequent runs with the same application number, each artifact is re-downloaded only if:

| Condition | Action |
|:---|:---|
| File is missing on disk | Download |
| File not recorded in manifest (new artifact type added) | Download |
| Middle bundle filename changed (e.g. new OA code added) | Download new filename |
| Document fingerprint changed (new doc added, date changed) | Re-download |
| No change detected | Skip |

At the end of each run a summary line is printed: `Summary: N downloaded, M skipped.`

To add a new downloadable artifact in the future, add a `_*_smart()` closure in `__main__` that calls `_needs_download()` + `_artifact_state[key] = {...}` and then calls the underlying download function. No other changes needed — `_finalize_manifest()` handles persistence automatically.

**API endpoints:**

| Method | Endpoint | Description |
|:---:|:---|:---|
| `GET` | `/resolve/{number}` | Resolve any input format → application number |
| `GET` | `/bundles/{app_no}` | Metadata + all bundles as JSON |
| `GET` | `/bundles/{app_no}/{index}/pdf` | Merged PDF stream for one bundle |
| `GET` | `/bundles/{app_no}/all.zip` | ZIP of all bundle PDFs + full patent PDF (`US{patent_no}.pdf`) |
| `GET` | `/bundles/{app_no}/patent.pdf` | Full granted patent PDF (Google Patents CDN) |

> `/resolve` accepts `force_patent=true` query param. All `/bundles/*` endpoints accept patent grant numbers (e.g. `US10902286`) in addition to application numbers.
> `show_extra` / `show_intclaim` flags apply to the CLI's `--separate-bundles` mode only; the server always uses 3-bundle mode with default-tier documents.

---

## 📖 The USPTO Prosecution Bundle Taxonomy

The tool maps every document in a patent application's file wrapper onto one of three archetypal bundle roles, derived from how USPTO document codes flow during examination:

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  BUNDLE 0 — Initial Claims          ❓ "What was claimed at filing?"        ║
║                                                                              ║
║  The operative CLM document filed with the application. If a Preliminary    ║
║  Amendment (A.PE) was filed within 7 days of the initial CLM, the amended   ║
║  CLM takes priority. Falls back to earliest INCOMING CLM, then earliest     ║
║  CLM of any direction.                                                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  BUNDLES 1–N — Office Action Rounds  ❓ "How did the examination unfold?"   ║
║                                                                              ║
║  One bundle per Non-Final (CTNF) or Final (CTFR) Office Action. Each bundle ║
║  anchors on the OA trigger, includes same-day support docs (prior art 892,  ║
║  form paragraphs FWCLM/SRFW/SRNT), and all applicant responses (REM, CLM,   ║
║  AMND, RCE, AFCP) filed before the next OA date. The final round also       ║
║  contains the Notice of Allowance (NOA / ISSUE.NOT).                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  BUNDLE N+1 — Granted Claims        ❓ "What was ultimately allowed?"       ║
║                                                                              ║
║  The last CLM document filed after the first OA, present only when a        ║
║  Notice of Allowance has been issued. Represents the final allowed claim set.║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## 🔧 Pipeline

The pipeline (core logic in `bundles_api.py`, HTTP layer in `bundles_server.py`) fetches prosecution metadata and documents from the USPTO Open API, classifies each document by code into ordered bundles, then either serves them as streaming HTTP responses (API mode) or writes merged PDFs to disk (CLI `--download` mode). The default path collapses all OA rounds into one middle bundle; `--separate-bundles` preserves the full per-round structure.

```mermaid
flowchart TD
    A([📥 Application Number · Patent Number · Formatted Input]) --> B

    B["🔍 Step 1 — resolve_application_number\nUS prefix + A1/A2/A9 → publication lookup · US prefix + B1/B2 → patent lookup\n--patent flag → patent lookup · Bare digits → try app, fallback to patent"]
    B -- "❌ Patent number not found" --> Z0([💥 404 — Cannot resolve patent number])
    B -- "✅ application number resolved" --> C

    C["📡 Step 2 — _get_metadata\nGET /meta-data — title, status, inventors, CPC, grant info"]
    C -- "❌ Not found or API error" --> Z1([💥 404 — Application not found])
    C -- "✅ metadata retrieved" --> D

    D["📡 Step 3 — _get_documents\nGET /documents — all doc codes, dates, PDF URLs, page counts"]
    D --> E

    E["📐 Step 4 — build_prosecution_bundles\nBundle 0: initial CLM · Bundles 1–N: OA rounds · Bundle N+1: granted CLM"]
    E --> F

    F{CLI Mode?}
    F -- "Default 3-bundle" --> G
    F -- "--separate-bundles" --> H

    G["🔄 Step 5 — _build_three_bundles\nCollapse all rounds into 3 groups: Initial / Prosecution / Granted"]
    G --> I

    H["📐 _filter_docs\nApply show_extra and show_intclaim visibility flags per bundle"]
    H --> I

    I{Output}
    I -- "JSON or --text" --> Z2([📤 JSON response or text table])
    I -- "--download" --> J

    J["📄 Step 6 — _merge_bundle_pdfs\nFetch individual PDFs from USPTO, merge with PyPDF2, add bookmarks"]
    J --> Z3([💾 Merged PDFs saved to output directory])
```

---

## 🧩 Step-by-Step Breakdown

### Step 1: resolve_application_number

Normalizes and resolves any input string — application number, patent grant number, or formatted variant — to a clean USPTO application number string.

- **Input:** Any of: application number (`16123456`), formatted number (`16/123,456`), patent grant number (`US10902286`), grant number with kind code (`US11973593B2`), pre-grant publication number (`US20210367709A1`), bare patent digits (`11973593`)
- **Output:** Digits-only application number string (e.g., `"16123456"`)
- **Raises:** `ValueError` when the number cannot be resolved

**Resolution order:**

| Condition | Action |
|:---|:---|
| Input has `US` prefix + publication kind code (`A1`/`A2`/`A9`) | Pass full string to `resolve_publication_to_application()` — queries `earliestPublicationNumber` |
| Input has `US` prefix without publication kind code | Strip prefix + kind code via `_extract_patent_digits()`, query `patentNumber` via `resolve_patent_to_application()` |
| `--patent` / `force_patent=true` | Treat bare digits as patent grant number, query `patentNumber` directly |
| Bare digits (no prefix, no flag) | Try as application number (`GET /meta-data`); if not found, fall back to patent lookup |

**`_extract_patent_digits()` normalization (used for grant numbers only):**

```
US11973593B2  →  strip US  →  11973593B2  →  strip kind code B2  →  11973593
US10902286    →  strip US  →  10902286    →  no kind code        →  10902286
16/123,456    →  strip / , →  16123456    →  (no US prefix)      →  16123456
```

**USPTO patent-to-application search query (grant numbers):**
```
GET /api/v1/patent/applications/search
  ?q=applicationMetaData.patentNumber:{digits}
  &fields=applicationNumberText,applicationMetaData.patentNumber
  &limit=1
```

**USPTO publication-to-application search query (pre-grant publications):**
```
GET /api/v1/patent/applications/search
  ?q=applicationMetaData.earliestPublicationNumber:{full_pub_number}
  &fields=applicationNumberText,applicationMetaData.earliestPublicationNumber
  &limit=1
```

> Note: `earliestPublicationNumber` stores the complete string including `US` prefix and kind code (e.g. `US20210367709A1`). Stripping them before querying returns a 404.

> **Examples:**
> `"US10902286"` → patent lookup → `"16123456"` ·
> `"US11973593B2"` → kind code stripped → patent lookup → application number ·
> `"US20210367709A1"` → publication lookup (full string) → `"16975325"` ·
> `"16/123,456"` → commas/slashes stripped → `"16123456"` (no API call needed)

---

### Step 2: _get_metadata

Calls `GET {BASE_API}/{app_no}/meta-data` with exponential-backoff retry and extracts structured application metadata from the nested `patentFileWrapperDataBag[0].applicationMetaData` response object.

- **Input:** Normalized application number string
- **Output:** Dict with keys: `application_number`, `title`, `status`, `filing_date`, `examiner`, `art_unit`, `docket`, `entity_status`, `app_type`, `patent_number`, `grant_date`, `pub_number`, `pub_date`, `cpc_codes`, `inventors`, `applicants` — or `None` on 404/error
- **Logic:** Pulls inventor city + country from `correspondenceAddressBag[0]`; returns `None` if `patentFileWrapperDataBag` key is absent from the response

> **Example fields:** `title: "Method for Wireless Communication"`, `status: "Patented Case"`, `patent_number: "11234567"`, `examiner: "SMITH, JOHN A"`

---

### Step 3: _get_documents

Calls `GET {BASE_API}/{app_no}/documents` and normalizes each entry in `documentBag` into a flat dict. Results are sorted newest-first by `officialDate`.

- **Input:** Normalized application number string
- **Output:** List of dicts, each with: `code`, `desc`, `date`, `direction`, `pages`, `pdf_url`, `files`
- **Logic:** If `downloadOptionBag` is present, extracts the PDF URL directly; otherwise constructs a fallback: `https://api.uspto.gov/api/v1/download/applications/{app_no}/{doc_id}.pdf`. Normalizes `MS_WORD` MIME type to `DOCX`. Page count falls back to `downloadOptionBag[0].pageTotalQuantity` if `pageCount` is absent.

> **Example entry:** `{"code": "CTNF", "desc": "Non-Final Rejection", "date": "2022-03-15", "direction": "OUTGOING", "pages": 12, "pdf_url": "https://..."}`

---

### Step 4: build_prosecution_bundles

The core classification engine. Iterates over all documents chronologically, anchors each bundle on an OA trigger code, groups same-day support docs and subsequent responses into the same round, and bookends the list with the initial and granted claim bundles.

- **Input:** Normalized application number (calls `_get_documents` and `_find_initial_claims` internally)
- **Output:** List of bundle dicts: `{index, label, type, documents}`
- **Logic:**
  1. **Bundle 0 (initial):** `_find_initial_claims()` selects the operative CLM — prefers CLM within 7 days of a Preliminary Amendment (A.PE), then earliest INCOMING CLM, then earliest CLM of any direction
  2. **Bundles 1–N (round / final_round):** For each CTNF/CTFR anchor, collects same-day `OA_SUPPORTING_CODES` sorted by `_OA_CODE_ORDER`, then all `RESPONSE_CODES | ADVISORY_CODES` dated after the OA and before the next OA
  3. **Bundle N+1 (granted):** The last CLM after the first OA date, only when `NOA_CODES` documents exist

**OA support document ordering (`_OA_CODE_ORDER`):**

| Code | Sort Order |
|:---:|:---:|
| `CTNF` / `CTFR` | 0 |
| `892` | 1 |
| `FWCLM` | 2 |
| `SRFW` | 3 |
| `SRNT` | 4 |

> **Example:** An application with 2 OAs produces 4 bundles: `Bundle 0 — Initial Claims`, `Bundle 1 — Round 1 (Non-Final)`, `Bundle 2 — Round 2 (Final) + NOA`, `Bundle 3 — Granted Claims`

---

### Step 5: _build_three_bundles

Collapses the variable-length per-round bundle list from Step 4 into exactly 3 logical groups for the default output mode.

- **Input:** List of bundles from `build_prosecution_bundles()`
- **Output:** List of exactly 3 dicts: `{label, filename, type, documents}`
- **Logic:**
  - **Group 0 (initial):** `initial` bundle documents as-is
  - **Group 1 (prosecution):** All `default`-tier documents from every `round` and `final_round` bundle, merged and sorted by date; the filename is built from whichever of `[REM, CTNF, CTFR, NOA]` are actually present (e.g., `REM-CTNF-NOA`). `ISSUE.NOT` is counted as `NOA` for naming.
  - **Group 2 (granted):** `granted` bundle documents as-is; empty list if not yet granted

> **Example filename for group 1:** Prosecution containing REM + CTNF + NOA → `REM-CTNF-NOA.pdf`

---

### Step 6: _merge_bundle_pdfs

Fetches each document PDF from its `pdf_url` and merges them into a single `io.BytesIO` stream using `PdfWriter`, adding a labeled PDF outline (bookmark) entry per document.

- **Input:** Bundle dict + `show_extra` bool + `show_intclaim` bool
- **Output:** `io.BytesIO` of the merged PDF, or raises `ValueError` if no valid PDFs were retrieved
- **Logic:** Calls `_filter_docs()` to apply visibility tiers; skips documents without a `pdf_url`; each fetched PDF is appended with a bookmark labeled `"{code} — {desc} ({date})"`. Raises `ValueError` when zero PDFs were fetched successfully.

**Filename sanitization regex (applied to bundle label before writing to disk):**

```regex
[^\w\s\-]
```

> **Example bookmark entry:** `"CTNF — Non-Final Rejection (2022-03-15)"` as a PDF outline item in the merged file

---

### Step 7: get_patent_pdf_url

Discovers the full-text granted patent PDF by scraping the Google Patents page for the patent number, trying multiple publication kind codes in sequence.

- **Input:** Patent number string (e.g., `"11234567"`)
- **Output:** Full CDN URL (`https://patentimages.storage.googleapis.com/...`) or `None`
- **Logic:** Iterates over kind codes `["B2", "B1", ""]`; for each, GETs `https://patents.google.com/patent/US{patent_number}{kind_code}/en` and extracts the CDN path via `re.findall`. Returns the first match found.

**Google Patents CDN URL extraction pattern:**

```regex
patentimages\.storage\.googleapis\.com/([a-f0-9/]+/US{patent_number}\.pdf)
```

> **Example:** For patent `11234567` — tries `US11234567B2`, then `US11234567B1`, then `US11234567`; returns the first successfully extracted CDN URL

---

## Appendix A — Document Classification Codes

The following document code sets govern how documents are classified into bundle roles and visibility tiers. All constants live in `bundles_api.py` and are imported by `bundles_server.py`.

**OA Trigger Codes (`OA_TRIGGER_CODES`) — start a new prosecution round**

| Code | Description |
|:---:|:---|
| `CTNF` | Non-Final Office Action |
| `CTFR` | Final Office Action |

**OA Supporting Codes (`OA_SUPPORTING_CODES`) — same-day OA attachments, `extra` tier by default**

| Code | Description |
|:---:|:---|
| `892` | Prior Art — References Cited |
| `FWCLM` | Form Paragraphs — Claims |
| `SRFW` | Search Report — Forward Citations |
| `SRNT` | Search Report — Notes |

**Response Codes (`RESPONSE_CODES`) — applicant responses following an OA**

| Code | Description | Default Visible |
|:---:|:---|:---:|
| `REM` | Remarks | ✅ |
| `CLM` | Claims | ✅ (initial/granted) · `intclaim` (rounds) |
| `AMND` | Amendment | ❌ (`extra`) |
| `A.1`, `A.2`, `A.3`, `A...`, `A.NE`, `A.NE.AFCP` | Amendment variants | ❌ (`extra`) |
| `AMSB` | Amendment after Final | ❌ (`extra`) |
| `RCEX` | Request for Continued Examination | ❌ (`extra`) |
| `RCE` | RCE document | ❌ (`extra`) |
| `AFCP` | After Final Consideration Pilot | ❌ (`extra`) |

**Advisory Codes (`ADVISORY_CODES`) — `extra` tier**

| Code | Description |
|:---:|:---|
| `CTAV` | Advisory Action |

**Notice of Allowance Codes (`NOA_CODES`) — default visible**

| Code | Description |
|:---:|:---|
| `NOA` | Notice of Allowance |
| `ISSUE.NOT` | Issue Notification (treated as NOA for naming purposes) |

---

## Appendix B — Bundle Types and Visibility Tiers

**Bundle Types**

| Type | Index | Description |
|:---:|:---:|:---|
| `initial` | 0 | Operative initial CLM filed with the application |
| `round` | 1 to N−1 | Intermediate OA + response round |
| `final_round` | N | Last OA round; also contains the NOA when present |
| `granted` | N+1 | Last CLM after first OA; only present when NOA was issued |

**Document Visibility Tiers**

| Tier | Shown by Default | CLI Flag | API Query Param | Applies To |
|:---:|:---:|:---|:---|:---|
| `default` | ✅ | — | — | CTNF, CTFR, NOA, ISSUE.NOT, REM; CLM in `initial`/`granted` bundles |
| `intclaim` | ❌ | `--show-intclaim` | `show_intclaim=true` | CLM docs inside `round` / `final_round` bundles |
| `extra` | ❌ | `--show-extra` | `show_extra=true` | OA support (892, FWCLM, SRFW, SRNT), AMND variants, CTAV, RCE, AFCP variants |

---

## 🇪🇺 EP (European Patent) Support

Same pipeline, same bundle shape, same CLI ergonomics — adapted for the European Patent Office. EP uses two data sources:

| Source | Purpose |
|:---|:---|
| **EPO OPS** (`ops.epo.org`) | OAuth2 bibliographic data (title, inventors, IPC) + app-number resolution |
| **EPO Register** (`register.epo.org`) | Document list + prosecution PDFs (scraped, session-based) |

The OPS API does **not** expose prosecution PDFs, so the register website is scraped behind a Cloudflare session. Each run warms the session once and reuses the same `JSESSIONID` + `__cf_bm` cookies for every PDF download.

### Setup

Register at [developers.epo.org](https://developers.epo.org), create an app, then add the credentials to `.env`:

```bash
EPO_CLIENT_ID=your_consumer_key
EPO_CLIENT_SECRET=your_consumer_secret
```

Extra dependencies beyond the USPTO set:

```bash
pip install beautifulsoup4 tqdm
```

### CLI usage

```bash
# 3-bundle mode (default) — JSON output
python bundles_api_ep.py EP2985974

# Human-readable text listing
python bundles_api_ep.py EP2985974 --text

# Dry-run: show every document + classification without downloading or bundling.
# Use this to verify which docs will land in which bundle before committing.
python bundles_api_ep.py EP2985974 --list-docs

# Download all 3 bundles as merged PDFs
python bundles_api_ep.py EP2985974 --download --output-dir ./ep_pdfs

# One PDF per prosecution round
python bundles_api_ep.py EP2985974 --separate-bundles --download

# Include supporting admin docs (delivery notes, receipts, minutes, oral-proc prep)
python bundles_api_ep.py EP2985974 --show-extra --text

# Include intermediate claim docs filed during prosecution
python bundles_api_ep.py EP2985974 --show-intclaim --text

# Different input formats
python bundles_api_ep.py 10173239                 # bare EP application number
python bundles_api_ep.py EP10173239.4             # check digit stripped
python bundles_api_ep.py EP3456789B1              # kind code stripped
python bundles_api_ep.py WO2015077217             # best-effort PCT/WO → EP lookup
```

### Input formats accepted

| Input | What happens |
|:---|:---|
| `EP2420929` | EP publication number → OPS register biblio → app `EP10173239` |
| `EP3456789A1` / `B1` | kind code stripped → treated as publication number |
| `10173239` | 8-digit EP application number (used directly) |
| `EP10173239.4` | check digit stripped → `EP10173239` |
| `WO2015077217` | PCT/WO publication → OPS family lookup → EP app (best-effort) |
| `PCT/US2020/012345` | PCT application reference stripped to digits |

### PCT-route (international phase) support

For PCT-route EP patents, the **Initial / International** bundle automatically includes:

- Filing documents (Claims, Description, Abstract, Drawings)
- ISA Written Opinion (all parts — cover sheet, boxes I-VIII, supplemental box)
- Copy of the International Search Report (ISR)
- International Preliminary Examination Report (IPER, Chapter II patents)
- Request for entry into the European phase
- Pre-examination amendments (Art.19, Art.34, pre-exam)

Direct-filed EP patents get the European Search Report + Search Opinion in the initial bundle instead. Everything after "Communication from the Examining Division" goes into the prosecution rounds; "Intention to grant" + "Decision to grant" go into the final granted bundle.

### API endpoints (EP)

| Method | Endpoint | Description |
|:---:|:---|:---|
| `GET` | `/ep/resolve/{number}` | Resolve EP/WO number → application number |
| `GET` | `/ep/bundles/{number}` | Metadata + 3-bundle prosecution view (JSON) |
| `GET` | `/ep/bundles/{number}/{index}/pdf` | Merged PDF stream for one bundle |
| `GET` | `/ep/bundles/{number}/all.zip` | ZIP of all 3 bundle PDFs |

Query params `show_extra=true` and `show_intclaim=true` control document visibility tiers, same semantics as the USPTO endpoints.

### Module layout

```
ep/
├── config.py            ← DOCUMENT TYPE CLASSIFICATIONS — edit this file to
│                          change what goes into each bundle
├── auth.py              ← EPO OPS OAuth2 token manager (auto-refreshes every 20 min)
├── ops_client.py        ← OPS API: biblio, register biblio, procedural-steps
├── register_client.py   ← register.epo.org scraper (session + doclist + PDFs)
├── resolver.py          ← EP pub / WO-PCT → app-number resolution
├── bundles.py           ← prosecution bundle builder + 3-bundle collapse
├── pdf.py               ← session-aware PDF merging (PyPDF2 + bookmarks)
└── __init__.py
```

### Customising what gets downloaded

Document classifications live in **[`ep/config.py`](ep/config.py)**. Unlike USPTO's short codes (CTNF, REM, ...), EPO Register documents have English text titles. The config uses **case-insensitive substring matching** against these titles.

To change how a doc type is classified, edit the relevant set:

```python
OA_TRIGGER_TYPES  = {...}   # Start a new prosecution round
RESPONSE_TYPES    = {...}   # Applicant responses (close a round)
SEARCH_TYPES      = {...}   # ESR / ESO / ISR / WOISA / IPER
FILING_TYPES      = {...}   # Filing application docs
GRANT_TYPES       = {...}   # Intention to grant + decision to grant
REFUSAL_TYPES     = {...}   # Decision to refuse
EXTRA_TYPES       = {...}   # Supporting admin (delivery, receipts, minutes, …)
```

After editing, run `--list-docs` to verify the new classification before downloading:

```bash
python bundles_api_ep.py EP2985974 --list-docs
# Shows: Date | Code | Tier | Procedure | Type — for every document
```

### EP document short codes (auto-derived from type)

| Code | Classified as | Triggered by (substring match) |
|:---:|:---|:---|
| `ESR` / `EESR` / `SESR` | SEARCH | European / Extended / Supplementary European search report |
| `ESO` | SEARCH | European search opinion |
| `ISR` / `WOISA` / `IPER` | SEARCH | International Search Report, Written Opinion of ISA, Int'l Preliminary Exam Report |
| `OA` / `SUMMON` | OA_TRIGGER | Communication from the Examining Division; Summons to oral proceedings |
| `RESP` / `AMND` | RESPONSE | Reply to communication; Amended claims / description |
| `GRANT` | GRANT | Intention to grant; Decision to grant; Mention of the grant |
| `REFUSE` | REFUSAL | Decision to refuse; Application deemed to be withdrawn |
| `CLM` / `DESC` / `ABS` / `DRW` | FILING_EXACT | Claims / Description / Abstract / Drawings (exact match) |
| `FILE` | FILING | Request for grant of a European patent; Request for entry into European phase |
| `MISC` | EXTRA | Everything else (delivery notes, receipts, fees, admin) |

### Key differences from the USPTO pipeline

| Aspect | USPTO | EPO |
|:---|:---|:---|
| Auth | Static API key (header) | OAuth2 (token exchange, auto-refresh every 20 min) |
| PDF delivery | Direct PDF URLs via API | Session-cookie flow on register.epo.org (Cloudflare-protected) |
| Doc identification | Short codes (CTNF, REM, NOA, …) | English text titles (substring-matched via `ep/config.py`) |
| Number formats | App number, US grant, US pub | EP app, EP pub (A1/B1), WO/PCT publication |
| PCT handling | N/A | Dedicated "International Searching Authority" + "PCT Chapter 2" procedure phases surface in the Initial bundle |

---

# EP Patent Download Process

Plain-English description of how `bundles_api_ep.py` fetches and produces EPO prosecution bundles.

---

## Overview

Given an EP patent number (e.g. `EP2985974`, `EP3444817`, or a WO/PCT number), the system:

1. Resolves the input to an EP application number + publication number
2. Fetches the document list for that application
3. Groups documents into up to four bundles (Initial Claims, Prosecution, Granted Claims, Patent Document)
4. Downloads each bundle as a merged PDF
5. Saves everything to `./ep_patents/<folder>/`

---

## Step 1 — Input Resolution

`ep/resolver.py` normalises whatever the user typed into a canonical `(app_no, pub_no)` pair.

- `EP2985974` → publication number → OPS `published-data/register` biblio call → `app_no = 16840831`, `pub_no = EP2985974B1`
- `WO2017012345` → same flow, PCT entry number → EP application number
- Short-form 8-digit OPS numbers (`YYSSSSSS`) and long-form 11-digit numbers (`YYYY0SSSSSS`) are both handled and normalised to short form for internal use.

The OPS API call uses OAuth2 — `ep/auth.py` keeps a thread-safe token cache and refreshes 60 seconds before expiry (tokens last 1200 seconds). Credentials come from `EPO_CLIENT_ID` / `EPO_CLIENT_SECRET` in `.env`.

---

## Step 2 — Document List

`_fetch_doclist()` in `bundles_api_ep.py` retrieves the list of prosecution documents. Two sources are tried in order:

### Primary: EPO Register

`ep/register_client.py::RegisterSession.list_documents(app_no)` fetches from `register.epo.org`.

**Why this works without Cloudflare blocking:**
EPO Register sits behind Cloudflare. Cloudflare's managed challenge specifically targets headless Chromium — it detects `navigator.webdriver`, the Chrome DevTools Protocol (CDP) fingerprint, and certain HTTP/2 header patterns unique to headless Chrome. A plain `requests` session with a Firefox/Gecko `User-Agent` has none of those signals, so Cloudflare lets it through without a challenge.

What the session does:
1. Sends a single GET to `https://register.epo.org/application?number=EP{app_no}&tab=doclist` using the Firefox UA.
2. EPO Register sets a `JSESSIONID` cookie in the response.
3. The response HTML is parsed (BeautifulSoup) to extract the document table — doc ID, type, date, page count for each document.

That `JSESSIONID` is all that's needed for every subsequent PDF download.

### Fallback: KOPD

If EPO Register fails (network error, empty list, etc.), `ep/kopd_client.py` queries KOPD (KIPO Open Patent Database at `kopd.kipo.go.kr:8888`).

KOPD proxies EPO's internal SOAP API. It has no Cloudflare in front of it. The query is a `POST /kipi/getDocList2.do` with form field `docdbNum=EP.<app_no>.A`. The `.A` suffix tells KOPD the input is an application number (not a publication number).

KOPD requires TLS 1.2 (rejects TLS 1.3 handshakes), so the client pins TLS 1.2 via a custom `HTTPAdapter`. KOPD rate-limits aggressively after ~5 quick requests — the client backs off at 2/4/8 seconds.

**Every doc dict is tagged** with `_source: "epo"` or `_source: "kopd"` so the PDF downloader later knows which backend to use for each document.

---

## Step 3 — Bundle Grouping

`ep/bundles.py::build_four_bundles(docs)` sorts and groups the document list into four bundle types:

| Bundle | Filename | What goes in it |
|---|---|---|
| `initial` | `Initial_claims.pdf` | "Claims" documents filed at EP entry |
| `round` | `Prosecution.pdf` | Everything else: search reports, OA rounds, replies, amendments |
| `granted` | `Granted_claims.pdf` | Last "Amended claims" before text-for-grant |
| `patent_document` | `Patent_document.pdf` | "Text intended for grant (clean copy)" |

Each document is classified by `ep/config.py::classify()` which assigns a tier (`OA`, `RESP`, `GRANT`, `SEARCH`, `FILING`, etc.) and a short display code. Precedence order: `EXTRA → OA → RESP → GRANT → REFUSE → SEARCH → FILING → FILING_EXACT → extra`.

---

## Step 4 — PDF Downloads

Each bundle is a list of doc IDs. For each doc, the system tries paths in this order:

### Path A — Whole-document POST (fastest, ~1–2s per doc)

`RegisterSession._post_fetch_pdf(doc_id, app_no)` sends:

```
POST https://register.epo.org/application
Content-Type: application/x-www-form-urlencoded

documentIdentifiers=<doc_id>&number=EP<app_no>
```

EPO Register returns the **entire multi-page document as a single PDF** in one response — no matter how many pages it has. This is 50–60× faster than fetching pages individually.

If this returns 403, the session is discarded, a new one is created, `warm()` is called again to get a fresh `JSESSIONID`, and the POST is retried once.

### Path B — Smart parallel + sequential page fetch (fallback)

`RegisterSession._fetch_pages_smart(doc_id, app_no, n_pages)` fetches each page individually from:

```
GET https://register.epo.org/application?documentId=<doc_id>&number=EP<app_no>&pageNr=<N>
```

Phase 1 — **fast-fail parallel**: 2 workers, no retry waits on failure. Successfully fetched pages are kept; failures are noted.

Phase 2 — **sequential retry**: failed pages only, with re-warm + 30s wait on each. Never re-fetches pages that already succeeded.

This ensures no work is lost if a page 403s mid-document.

### Path C — KOPD re-probe (bundle-level fallback)

If **every** document in a bundle fails EPO Register fetches (both Path A and B), `ep/pdf.py` resets KOPD's reachability cache and tries fetching the bundle from KOPD instead. KOPD docs are matched to EPO Register docs by `(date, doc_type)`.

### Path D — One page at a time, sequential (last resort)

Built into `_fetch_pages_smart` when both phases above fail — falls back to sequential single-threaded page fetching. Slowest but most resilient.

---

## Step 5 — Granted Claims PDF

`Granted_claims.pdf` gets special treatment because the EPO Register version of "Amended claims" can differ from the published grant. Three sources are tried in order:

1. **PCS** (`us/pcs_api.py`) — Dolcera's PCS proxy. Queries by exact `EP-{pub_no}-{kind_code}` (e.g. `EP-3337077-B1`). Returns clean claim XML which is rendered into a formatted PDF. Requires `PCS_API_KEY` in `.env`.

2. **KOPD** — If the doc list came from KOPD and KOPD is reachable, downloads the granted bundle PDFs directly from KOPD's `/docContent/download.do` endpoint.

3. **EPO Register merge** — Falls back to `ep/pdf.merge_bundle_pdfs`, which downloads the "Amended claims with annotations" documents through EPO Register using Paths A/B/C above.

---

## Step 6 — Output Layout

With `--download`, files land under `./ep_patents/` by default (override with `--output-dir`):

```
ep_patents/
  EP16840831/
    Initial_claims.pdf
    Prosecution.pdf
    Granted_claims.pdf
    Patent_document.pdf
    manifest.json          ← tracks what was downloaded + fingerprints for dedup
```

`manifest.json` fingerprints each artifact so re-runs skip files already present. A source change (e.g. enabling PCS) flips the fingerprint and forces a one-time re-fetch.

---

## Step 7 — Divisionals (`--divisionals` flag)

If the input is itself a divisional (split off from a parent EP application), the `--divisionals` flag walks the parent chain upward:

1. OPS register biblio returns `<reg:related-documents>/<reg:division>/<reg:parent-doc>` entries — each one is an ancestor application number.
2. For each ancestor: resolve `(app_no, pub_no)`, fetch its doc list, group bundles, download PDFs into its own sibling folder `ep_patents/EP{ancestor_app_no}/`.
3. Walk continues upward — parent's parent, etc. — up to 10 levels deep, with a cycle guard.
4. `related.json` in the main folder records the full ancestor chain.

Child divisionals / sibling divisionals are **not** followed. This feature is strictly "fetch upstream context."

---

## Credentials Required

```
EPO_CLIENT_ID=...       # developers.epo.org
EPO_CLIENT_SECRET=...   # developers.epo.org
PCS_API_KEY=...         # Dolcera PCS proxy (optional — skipped if absent)
```

---

## Summary of Speed Improvements

| Approach | ~38-page doc | Notes |
|---|---|---|
| Old: page-by-page sequential | ~110s | 1 GET per page |
| Old: 3 parallel workers | ~247s | CF rate-limits, triggers 20/60s waits |
| New: POST whole-doc (Path A) | ~1–2s | One request, one PDF |
| New: smart parallel fallback | ~30–40s | 2 workers + sequential retry for failures only |

Path A alone is a 50–60× speedup over the original approach.

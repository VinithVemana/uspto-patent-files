# ep/ — EP (European Patent) Prosecution Bundle Module

Core logic for fetching and organizing European Patent prosecution bundles.
CLI entry point is `bundles_api_ep.py` at the project root.

EPO OPS does **not** expose prosecution PDFs. Two sources are available:
- **KOPD** (`kopd.kipo.go.kr:8888`) — primary. No Cloudflare, plain HTTPS+TLS1.2.
- **EPO Register** (`register.epo.org`) — fallback. Behind Cloudflare; needs `RegisterSession` session warming.

## Module Files

| File | Purpose |
|---|---|
| `config.py` | **User-editable** doc-type → tier classifications. `classify()` assigns tiers; `short_code()` gives 4-6 char display codes. |
| `auth.py` | Thread-safe OAuth2 token cache for OPS. Refreshes 60 s before expiry (tokens live 1200 s). Reads `EPO_CLIENT_ID` / `EPO_CLIENT_SECRET` from `.env`. |
| `ops_client.py` | OPS API: `get_publication_biblio()`, `get_register_biblio()`, `extract_metadata()`, `extract_application_number()`, `extract_divisional_parent()` + `extract_divisional_children()` (walk `<reg:related-documents>/<reg:division>` for `--divisionals` — upward and downward respectively). |
| `kopd_client.py` | KOPD (KIPO Open Patent Database) doc fetcher. TLS 1.2 pinned `HTTPAdapter`, `is_reachable()` TCP probe, `list_documents(app_no)`, `fetch_doc_pdf(doc)`, `merge_bundle_pdfs(bundle)`. Primary EP doclist source; sidesteps Cloudflare. |
| `register_client.py` | `RegisterSession`: warms Cloudflare session, parses doclist HTML, fetches PDFs. Re-warms and retries once if a PDF response isn't `%PDF-`. Fallback when KOPD is unreachable / soft-fails. |
| `resolver.py` | Input normalization + EP/WO/PCT → application-number resolution. |
| `bundles.py` | Bundle builder. `_is_oa_trigger()` restricts to `Search / examination` procedure. |
| `pdf.py` | `merge_bundle_pdfs(session, bundle, app_no, ...)` — uses shared session so Cloudflare cookies persist across calls. EPO Register backend only; KOPD has its own merger. |

## Data Flow

```
Input (EP app / EP pub / WO-PCT)
  → resolver.resolve()                       # normalize + pub→app via OPS register biblio
  → ops_client.get_publication_biblio()      # OAuth2 biblio metadata
  → bundles_api_ep._fetch_doclist()          # KOPD → EPO Register fallback
      kopd_client.list_documents(app_no)     #   primary  — POST /kipi/getDocList2.do
      register_client.RegisterSession        #   fallback — warm CF session + BS4 parse
  → bundles.build_prosecution_bundles()      # group by procedure + round
  → bundles.build_four_bundles()             # collapse to {initial, round, granted, patent_document}
  → per-doc dispatch on `_source`:
      kopd_client.merge_bundle_pdfs(bundle)  # KOPD-sourced docs
      pdf.merge_bundle_pdfs(session, ...)    # EPO-sourced docs
```

Each doc dict from `_fetch_doclist` is tagged with `_source: "kopd"` or `_source: "epo"`. KOPD-sourced docs also carry a `_kopd` sub-dict with the raw fields the KOPD download endpoint needs (`docid`, `docformat`, `rs_dt`, `rs_doc_nm`, `numberOfPage`, `docdb`). `bundles.build_four_bundles()` preserves these passthrough fields when annotating with `code` / `direction` / `category`.

### Divisional ancestor walk (`--divisionals`)

`bundles_api_ep._walk_divisional_ancestors(app_no, pub_no)` recursively follows `<reg:parent-doc>` upward via one OPS register-biblio call per ancestor (USPTO's `/continuity` endpoint has no EP analog). For each ancestor, `_process_divisionals` resolves `(app_no, pub_no)` via `resolver.resolve()` (handling both short-form 8-digit and long-form 11-digit OPS app numbers), then reuses `_fetch_meta_and_doclist` + `_download_bundles` to produce a fully-equivalent sibling folder under the shared root. `_save_related` writes `related.json` summarising the ancestor chain in the main folder. Walk capped at 10 levels, with a visited-app cycle guard.

`extract_divisional_children` is exposed in `ops_client.py` but not currently consumed — the CLI's `--divisionals` flag is strictly upward-only. The helper is kept for diagnostics and potential future child-walk features.

## Bundle Types

### Default mode — `build_four_bundles()` (4 PDFs)

| Bundle | Filename | Contents |
|---|---|---|
| **Initial Claims** (`initial`) | `initial_claims.pdf` | Bare "Claims" doc(s) filed at EP entry |
| **Prosecution** (`round`) | `prosecution.pdf` | Everything else: ISR, ESR, OA rounds, replies, intermediate amendments |
| **Granted Claims** (`granted`) | `granted_claims.pdf` | Last "Amended claims" doc before text-for-grant |
| **Patent Document** (`patent_document`) | `patent_document.pdf` | "Text intended for grant (clean copy)" |

### Separate-bundles mode — `build_prosecution_bundles()` (one PDF per OA round)

| Bundle | Contents |
|---|---|
| **Initial** (`initial`) | Filing docs + ESR/ESO (direct-EP) OR WOISA + ISR + IPER (PCT-route) + pre-exam amendments |
| **Round N** (`round` / `final_round`) | Each "Communication from the Examining Division" or "Summons" + applicant responses |
| **Granted** (`granted`) | "Intention to grant" + "Decision to grant" + "Text intended for grant" — or a Refused bundle |

## Credentials

Register at [developers.epo.org](https://developers.epo.org) and add to `.env`:
```
EPO_CLIENT_ID=...
EPO_CLIENT_SECRET=...
```

## classify() Precedence in config.py

`EXTRA → OA → RESP → GRANT → REFUSE → SEARCH → FILING → FILING_EXACT → extra`

To reclassify a document type, edit the sets in `config.py` — the precedence order determines which tier wins when a type appears in multiple sets.

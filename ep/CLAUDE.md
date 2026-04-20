# ep/ — EP (European Patent) Prosecution Bundle Module

Core logic for fetching and organizing European Patent prosecution bundles.
CLI entry point is `bundles_api_ep.py` at the project root.

EPO OPS does **not** expose prosecution PDFs — those live on `register.epo.org`
behind Cloudflare, which requires session warming via `RegisterSession`.

## Module Files

| File | Purpose |
|---|---|
| `config.py` | **User-editable** doc-type → tier classifications. `classify()` assigns tiers; `short_code()` gives 4-6 char display codes. |
| `auth.py` | Thread-safe OAuth2 token cache for OPS. Refreshes 60 s before expiry (tokens live 1200 s). Reads `EPO_CLIENT_ID` / `EPO_CLIENT_SECRET` from `.env`. |
| `ops_client.py` | OPS API: `get_publication_biblio()`, `get_register_biblio()`, `extract_metadata()`, `extract_application_number()`. |
| `register_client.py` | `RegisterSession`: warms Cloudflare session, parses doclist HTML, fetches PDFs. Re-warms and retries once if a PDF response isn't `%PDF-`. |
| `resolver.py` | Input normalization + EP/WO/PCT → application-number resolution. |
| `bundles.py` | Bundle builder. `_is_oa_trigger()` restricts to `Search / examination` procedure. |
| `pdf.py` | `merge_bundle_pdfs(session, bundle, app_no, ...)` — uses shared session so Cloudflare cookies persist across calls. |

## Data Flow

```
Input (EP app / EP pub / WO-PCT)
  → resolver.resolve()                       # normalize + pub→app via OPS register biblio
  → ops_client.get_publication_biblio()      # OAuth2 biblio metadata
  → register_client.RegisterSession          # warm Cloudflare session on doclist page
      .list_documents()                      # BeautifulSoup parse of doclist HTML
  → bundles.build_prosecution_bundles()      # group by procedure + round
  → bundles.build_three_bundles()            # collapse to {initial, middle, granted}
  → pdf.merge_bundle_pdfs(session, ...)      # session-based PDF fetch + PyPDF2 merge
```

## Bundle Types

| Bundle | Contents |
|---|---|
| **Initial** (`initial`) | Filing docs + ESR/ESO (direct-EP) OR WOISA + ISR + IPER (PCT-route) + pre-exam amendments |
| **Round N** (`round` / `final_round`) | Each "Communication from the Examining Division" or "Summons" + applicant responses |
| **Granted** (`granted`) | "Intention to grant" + "Decision to grant" — or a Refused bundle |

3-bundle filename built from `_MIDDLE_CODE_ORDER = ["RESP","OA","SUMMON","GRANT","REFUSE"]`.

## Credentials

Register at [developers.epo.org](https://developers.epo.org) and add to `.env`:
```
EPO_CLIENT_ID=...
EPO_CLIENT_SECRET=...
```

## classify() Precedence in config.py

`EXTRA → OA → RESP → GRANT → REFUSE → SEARCH → FILING → FILING_EXACT → extra`

To reclassify a document type, edit the sets in `config.py` — the precedence order determines which tier wins when a type appears in multiple sets.

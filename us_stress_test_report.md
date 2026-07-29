# US Bundle Download Stress Test — 9 patents (2026-07-29)

Harness: `us_stress_test.py` — serial, one `bundles_api.py <no> --download` subprocess per patent,
every stdout/stderr line timestamped. Cold cache (fresh output dir, no manifest reuse).
Output: `us_stress_test_out/`, logs in `us_stress_test_out/_logs/`.

## Per-patent results

| patent | time (s) | files | MB | folder | notes |
|---|---:|---:|---:|---|---|
| US-12652718-B2 | 28.9 | 4 | 1.99 | `US12652718` | 1 failure — Granted_document |
| US-11122575-B2 | 42.0 | 5 | 4.37 | `US11122575` | slowest granted |
| US-12335844-B1 | 28.2 | 5 | 2.51 | `US12335844` | |
| US-12232007-B2 | 32.4 | 5 | 3.88 | `US12232007` | |
| US-20240089837-A1 | 47.3 | 3 | 3.33 | `app_17931825` | pre-grant pub → no granted bundles; 6 docs / 80 pages in round bundle |
| US-20250105992-A1 | 16.3 | 3 | 0.71 | `app_18725996` | fastest — 1 doc in round bundle |
| US-9307479-B2 | 41.7 | 5 | 3.53 | `US9307479` | 7 docs in round bundle |
| US-11546842-B2 | 33.7 | 5 | 3.81 | `US11546842` | |
| US-11528595-B2 | 31.1 | 5 | 3.16 | `US11528595` | |
| **TOTAL** | **301.5** | **40** | **27.3** | | 1 failure |

Mean 33.5 s/patent. Range 16.3–47.3 s. All exit code 0.

Granted patents get 5 files (`Initial_claims`, `REM-CTNF-NOA`, `Granted_claims`, `Index_of_claims`,
`Granted_document`); pre-grant pubs get 3 (no `Granted_claims` / `Granted_document` — correct).

## Stage breakdown (seconds)

| patent | total | resolve+meta | initial | round | granted_clm | index | granted_doc |
|---|---:|---:|---:|---:|---:|---:|---:|
| US11122575B2 | 41.9 | 2.3 | 3.0 | 25.9 | 2.6 | 3.7 | 2.9 |
| US11528595B2 | 31.0 | 2.4 | 3.2 | 15.0 | 2.6 | 3.8 | 2.8 |
| US11546842B2 | 33.5 | 2.7 | 2.3 | 18.0 | 2.7 | 3.9 | 2.7 |
| US12232007B2 | 32.3 | 2.6 | 3.0 | 16.2 | 2.6 | 3.8 | 2.7 |
| US12335844B1 | 28.0 | 2.5 | 3.4 | 10.8 | 2.6 | 3.9 | 3.4 |
| US12652718B2 | 28.8 | 2.5 | 3.8 | 12.7 | 2.7 | 3.6 | 1.8 |
| US20240089837A1 | 47.2 | 2.5 | 3.3 | 36.1 | — | 3.8 | — |
| US20250105992A1 | 16.2 | 2.2 | 2.8 | 6.0 | — | 3.7 | — |
| US9307479B2 | 41.6 | 3.1 | 2.8 | 24.8 | 2.9 | 4.0 | 2.7 |
| **SUM** | **300.6** | 22.9 | 27.6 | **165.4** | 18.6 | 34.4 | 19.0 |
| **share** | | 8 % | 10 % | **57 %** | 6 % | 12 % | 7 % |

## What is slow, and why

**`REM-CTNF-NOA` bundle = 57 % of all runtime.** `pdf._merge_bundle_pdfs` downloads each USPTO
file-wrapper doc **serially**. Round bundles hold 1–7 docs; runtime tracks doc **count**, not page
count or bytes:

| patent | round docs | round pages | round time | s/doc |
|---|---:|---:|---:|---:|
| US20250105992A1 | 1 | 16 | 6.0 | 6.0 |
| US12335844B1 | 3 | 18 | 10.8 | 3.6 |
| US11122575B2 | 6 | 46 | 25.9 | 4.3 |
| US20240089837A1 | 6 | 80 | 36.1 | 6.0 |
| US9307479B2 | 7 | 54 | 24.8 | 3.5 |

Measured directly against `api.uspto.gov` (the 6 docs of app 16894146):

```
serial   4.57 / 2.85 / 4.15 / 2.88 / 3.06 / 3.54 s  → 21.08 s total
parallel(6)                                        →  3.84 s total   (5.5×)
```

Per-request latency is ~3–4.5 s regardless of size (88 KB and 625 KB cost the same) — **latency-bound,
not bandwidth-bound**. 6 concurrent requests hit no rate limit and each individual request kept the
same latency.

Other stages are single-request floors, nothing pathological:
- `resolve+meta` 2.2–3.1 s — resolve lookup + `/meta-data` + `/documents`.
- `initial` 2.3–3.8 s and `index` 3.6–4.0 s — one doc fetch each.
- `granted_clm` 2.6–2.9 s — pcs_api hit (~1.3 s query, 0.03 s render); Dolcera is not a bottleneck.
- `granted_doc` 1.8–3.4 s — Google Patents page + PDF.

### Optimizations, in payoff order

1. **Thread the per-doc fetches inside `_merge_bundle_pdfs`** (`ThreadPoolExecutor(6)`, merge in
   original order). Round stage 165 s → ~30 s; total 301 s → ~165 s. Biggest single win.
2. **Run the 5 artifacts of one patent concurrently** — initial / round / granted_clm / index /
   granted_doc are independent. Combined with (1), per-patent wall clock ≈ resolve (2.5 s) + slowest
   artifact (~5 s) ≈ **8 s**.
3. **Patent-level parallelism** in the harness (2–3 at a time) — the EP test already tiers this way.

## Failure

`US12652718_Granted_document.pdf` — reason recorded as *"PDF URL not found on Google Patents (may be
bot-blocked)"*. Verified: **not** a bot block. `patents.google.com/patent/US12652718B2/en` (and `B1`,
and bare) all return **HTTP 404** with a 1445-byte body. The grant is too recent to be indexed. The
message should distinguish a real 404 from a 503/challenge — currently both collapse into the same
"may be bot-blocked" string.

Everything else: 0 failures, all manifests clean.

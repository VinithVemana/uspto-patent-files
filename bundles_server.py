"""
bundles_server.py — FastAPI hosting layer for prosecution bundles
=================================================================

Imports all core logic from bundles_api.py and exposes it over HTTP.

RUN
---
    uvicorn bundles_server:app --host 0.0.0.0 --port 7901

API ENDPOINTS
-------------
GET /resolve/{number}
  Resolve any input format to a USPTO application number.
  Query params:
    force_patent=true    — force patent-to-app lookup even without 'US' prefix
  Examples:
    /resolve/US10902286            → {"input": "US10902286", "application_number": "16123456"}
    /resolve/US11973593B2          → strips kind code, resolves via patent lookup
    /resolve/11973593?force_patent=true

GET /bundles/{application_number}
  Returns application metadata + all prosecution bundles.
  Default: only CTNF, CTFR, NOA, CLM, REM docs per bundle.
  Query params:
    show_extra=true      — also include OA support, amendments, advisory actions, RCE docs
    show_intclaim=true   — also include intermediate CLM docs in round bundles

GET /bundles/{application_number}/{bundle_index}/pdf
  Stream a merged PDF for one bundle.
  Same show_extra / show_intclaim flags apply to which docs are merged.

GET /bundles/{application_number}/all.zip
  Stream a ZIP of all bundle PDFs.
  Same show_extra / show_intclaim flags apply.

GET /bundles/{application_number}/index-of-claims.pdf
  Stream a merged PDF of all FWCLM (Index of Claims) documents.
  Returns 404 when no FWCLM docs exist for this application.

GET /bundles/{application_number}/patent.pdf
  Stream the full granted patent PDF (sourced from Google Patents CDN).
  Returns 404 if the application has not been granted or no PDF is found.

Note: all /bundles/* and /resolve/* endpoints accept patent grant numbers
(e.g. US10902286) or pre-grant publication numbers (e.g. US20210367709A1)
in addition to application numbers.


EP (European Patent) ENDPOINTS
------------------------------
All EP endpoints live under /ep/ and accept the same input formats the CLI does:
  EP application number    EP10173239  or  10173239
  EP publication number    EP3456789   or  EP3456789B1
  PCT / WO publication     WO2015077217  (best-effort EP family lookup)

GET /ep/resolve/{number}
  Resolve any input to an EP application number + publication number.

GET /ep/bundles/{number}
  Metadata + 3-bundle prosecution view (JSON).
  Query params:
    show_extra=true       Include supporting admin docs
    show_intclaim=true    Include intermediate claim docs in round bundles

GET /ep/bundles/{number}/{bundle_index}/pdf
  Stream a merged PDF for one of the 3 bundles.

GET /ep/bundles/{number}/all.zip
  Stream a ZIP of all 3 bundle PDFs.
"""

import io
import zipfile

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from us.resolver import resolve_application_number
from us.client import _get_metadata
from us.bundles import build_prosecution_bundles, _build_three_bundles
from us.pdf import get_patent_pdf_url, _merge_bundle_pdfs, _merge_fwclm_pdf
from us.config import HEADERS, GOOGLE_PATENTS_HEADERS

# --- EP module ---
from ep import bundles as ep_bundles
from ep import ops_client as ep_ops_client
from ep import pdf as ep_pdf
from ep import resolver as ep_resolver
from ep.register_client import RegisterSession

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Patent Prosecution Bundles API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/resolve/{number}")
def resolve_number(number: str, force_patent: bool = False):
    """
    Resolve a patent grant number or application number to a USPTO application number.

    Examples:
      /resolve/US10902286          → {"application_number": "16123456", ...}
      /resolve/US11973593B2        → strips kind code, resolves via patent lookup
      /resolve/16123456            → echoes back (already an application number)
      /resolve/11973593            → tries app number first, falls back to patent lookup
      /resolve/11973593?force_patent=true  → forces patent→app lookup

    Formatting variants accepted: commas, slashes, spaces are stripped automatically.
    """
    try:
        app_no = resolve_application_number(number, force_patent=force_patent)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"input": number, "application_number": app_no}


@app.get("/bundles/{application_number}")
def get_bundles(
    application_number: str,
    request: Request,
):
    """
    Return application metadata + prosecution bundles (3-bundle mode).

    Collapses all prosecution rounds into exactly 3 logical groups:
      0 — initial_claims     (operative initial CLM)
      1 — {REM-CTNF-...}     (all prosecution round docs, named from codes present)
      2 — granted_claims     (last CLM after grant)

    Each bundle includes a `filename` key matching the CLI's --download output name.
    """
    try:
        app_no = resolve_application_number(application_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    meta = _get_metadata(app_no)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Application {application_number} not found in USPTO")

    bundles = build_prosecution_bundles(app_no)
    if not bundles:
        raise HTTPException(status_code=404, detail="No prosecution documents found for this application")

    three = _build_three_bundles(bundles)
    base  = str(request.base_url).rstrip("/")

    result_bundles = []
    for i, b in enumerate(three):
        result_bundles.append({
            "index":        i,
            "label":        b["label"],
            "filename":     b["filename"],
            "type":         b["type"],
            "download_url": f"{base}/bundles/{app_no}/{i}/pdf",
            "documents":    b["documents"],
        })

    patent_no = meta.get("patent_number")
    return {
        **meta,
        "patent_pdf_url": f"{base}/bundles/{app_no}/patent.pdf" if patent_no else None,
        "bundles":        result_bundles,
    }


@app.get("/bundles/{application_number}/patent.pdf")
def download_patent_pdf(application_number: str):
    """
    Stream the full granted patent PDF.

    The PDF is fetched from the Google Patents CDN
    (patentimages.storage.googleapis.com) — no API key required.

    Returns 404 when:
      - the application number is not in USPTO
      - the application has not been granted (no patent number assigned)
      - the PDF is not yet available on Google Patents
    """
    try:
        app_no = resolve_application_number(application_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    meta = _get_metadata(app_no)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Application {application_number} not found in USPTO")

    patent_number = meta.get("patent_number")
    if not patent_number:
        raise HTTPException(
            status_code=404,
            detail="Application has not been granted (no patent number assigned yet)",
        )

    pdf_url = get_patent_pdf_url(patent_number)
    if not pdf_url:
        raise HTTPException(
            status_code=404,
            detail=f"Patent PDF not found on Google Patents for US{patent_number}",
        )

    try:
        r = requests.get(
            pdf_url,
            headers=GOOGLE_PATENTS_HEADERS,
            timeout=60,
            stream=True,
        )
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch patent PDF: {exc}")

    filename = f"US{patent_number}.pdf"
    return StreamingResponse(
        r.iter_content(chunk_size=65536),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/bundles/{application_number}/{bundle_index}/pdf")
def download_bundle_pdf(
    application_number: str,
    bundle_index: int,
):
    """
    Stream a merged PDF for one of the 3 prosecution bundles (indices 0–2).

    Matches the CLI's default 3-bundle mode:
      0 → initial_claims.pdf
      1 → {REM-CTNF-...}.pdf  (named from codes present)
      2 → granted_claims.pdf
    """
    try:
        app_no = resolve_application_number(application_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    bundles = build_prosecution_bundles(app_no)

    if not bundles:
        raise HTTPException(status_code=404, detail="No documents found for this application")

    three = _build_three_bundles(bundles)
    if bundle_index < 0 or bundle_index >= len(three):
        raise HTTPException(status_code=404, detail=f"Bundle {bundle_index} not found (total: {len(three)})")

    b = three[bundle_index]
    try:
        pdf = _merge_bundle_pdfs(
            {"type": b["type"], "documents": b["documents"]},
            show_extra=False,
            show_intclaim=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    filename = f"{b['filename']}.pdf"
    return StreamingResponse(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/bundles/{application_number}/index-of-claims.pdf")
def download_index_of_claims(application_number: str):
    """
    Stream a merged PDF of all FWCLM (Index of Claims) documents.
    Returns 404 when no FWCLM docs exist for this application.
    """
    try:
        app_no = resolve_application_number(application_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    bundles = build_prosecution_bundles(app_no)
    if not bundles:
        raise HTTPException(status_code=404, detail="No documents found for this application")

    try:
        pdf = _merge_fwclm_pdf(app_no)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return StreamingResponse(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="Index_of_claims.pdf"'},
    )


@app.get("/bundles/{application_number}/all.zip")
def download_all_bundles_zip(application_number: str):
    """
    Stream a ZIP of all bundle PDFs plus the full granted patent PDF.

    Matches the CLI's --download behavior exactly:
      initial_claims.pdf
      {REM-CTNF-...}.pdf
      granted_claims.pdf
      US{patent_no}.pdf    (only when the application has been granted)
    """
    try:
        app_no = resolve_application_number(application_number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    meta = _get_metadata(app_no)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Application {application_number} not found in USPTO")

    bundles = build_prosecution_bundles(app_no)
    if not bundles:
        raise HTTPException(status_code=404, detail="No documents found for this application")

    three   = _build_three_bundles(bundles)
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 3 prosecution bundle PDFs — mirrors CLI _download_three()
        for b in three:
            try:
                pdf = _merge_bundle_pdfs(
                    {"type": b["type"], "documents": b["documents"]},
                    show_extra=False,
                    show_intclaim=False,
                )
                zf.writestr(f"{b['filename']}.pdf", pdf.getvalue())
            except ValueError:
                pass   # skip bundles with no PDFs

        # Index of Claims PDF — mirrors CLI _download_index_of_claims()
        try:
            fwclm_pdf = _merge_fwclm_pdf(app_no)
            zf.writestr("Index_of_claims.pdf", fwclm_pdf.getvalue())
        except (ValueError, Exception):
            pass   # best effort — skip if no FWCLM docs

        # Full patent PDF — mirrors CLI _download_patent_pdf()
        patent_no = meta.get("patent_number")
        if patent_no:
            pdf_url = get_patent_pdf_url(patent_no)
            if pdf_url:
                try:
                    r = requests.get(
                        pdf_url,
                        headers=GOOGLE_PATENTS_HEADERS,
                        timeout=60,
                    )
                    if r.status_code == 200:
                        zf.writestr(f"US{patent_no}.pdf", r.content)
                except Exception:
                    pass   # best effort — skip if unavailable

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{app_no}_bundles.zip"'},
    )


# ===========================================================================
# EP (European Patent) routes
# ===========================================================================
#
# Mirrors the USPTO routes under an /ep/ prefix. EP prosecution documents
# live on register.epo.org (not OPS), so each endpoint opens a short-lived
# RegisterSession to warm Cloudflare cookies and scrape the document list.
#
# Accepts the same input formats the CLI does:
#   EP application number    EP10173239  or  10173239
#   EP publication number    EP3456789   or  EP3456789B1
#   PCT / WO publication     WO2015077217  (best-effort EP family lookup)
# ===========================================================================


def _resolve_ep_or_404(number: str) -> tuple[str, str | None]:
    try:
        return ep_resolver.resolve(number)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _fetch_ep_meta_and_docs(number: str):
    """
    Shared helper: resolve + fetch meta + document list.
    Returns (app_no, pub_no, metadata_dict, documents_list, session).
    Raises HTTPException on any failure.
    """
    app_no, pub_no = _resolve_ep_or_404(number)

    pub_biblio = ep_ops_client.get_publication_biblio(f"EP{pub_no}") if pub_no else None
    reg_biblio = ep_ops_client.get_register_biblio(f"EP{pub_no}") if pub_no else None
    meta = (ep_ops_client.extract_metadata(pub_biblio, reg_biblio)
            if pub_biblio else
            {"application_number": app_no, "publication_number": pub_no,
             "patent_number": None, "title": "N/A", "status": "N/A",
             "filing_date": "", "publication_date": "", "grant_date": None,
             "inventors": [], "applicants": [], "ipc_codes": []})
    meta["application_number"] = app_no

    session = RegisterSession()
    try:
        docs = session.list_documents(f"EP{app_no}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"EPO Register fetch failed: {e}")

    if not docs:
        raise HTTPException(
            status_code=404,
            detail=f"No documents found in EPO Register for EP{app_no}",
        )
    return app_no, pub_no, meta, docs, session


@app.get("/ep/resolve/{number}")
def ep_resolve_number(number: str):
    """
    Resolve an EP publication / WO (PCT) number to an EP application number.

    Examples:
      /ep/resolve/EP3456789       → {"application_number": "10173239", ...}
      /ep/resolve/EP3456789B1     → kind code stripped, same result
      /ep/resolve/WO2015077217    → best-effort EP family lookup
      /ep/resolve/10173239        → echoes back (already an app number)
    """
    app_no, pub_no = _resolve_ep_or_404(number)
    return {
        "input":              number,
        "application_number": app_no,
        "publication_number": pub_no,
    }


@app.get("/ep/bundles/{number}")
def ep_get_bundles(number: str, request: Request,
                   show_extra: bool = False, show_intclaim: bool = False):
    """
    Return EP application metadata + prosecution bundles (3-bundle mode).

    Query params:
      show_extra=true       Include supporting admin docs (delivery, receipts, minutes)
      show_intclaim=true    Include intermediate claim docs inside round bundles

    Collapses every prosecution round into 3 logical groups matching the CLI:
      0 — initial.pdf          (filing + ESR/ISR + pre-exam amendments)
      1 — {RESP-OA-...}.pdf    (all EP examination round docs, date-sorted)
      2 — granted_claims.pdf   (intention to grant + decision)
                               or refused.pdf if refused
    """
    app_no, pub_no, meta, docs, _ = _fetch_ep_meta_and_docs(number)

    bundles_list = ep_bundles.build_prosecution_bundles(docs)
    three        = ep_bundles.build_three_bundles(bundles_list)
    base         = str(request.base_url).rstrip("/")

    return {
        **meta,
        "bundles": [
            {
                "index":        i,
                "label":        b["label"],
                "filename":     b["filename"],
                "type":         b["type"],
                "download_url": f"{base}/ep/bundles/{app_no}/{i}/pdf",
                "documents":    ep_bundles._filter_docs(
                    b["documents"], show_extra=show_extra, show_intclaim=show_intclaim
                ),
            }
            for i, b in enumerate(three)
        ],
    }


@app.get("/ep/bundles/{number}/{bundle_index}/pdf")
def ep_download_bundle_pdf(number: str, bundle_index: int,
                           show_extra: bool = False, show_intclaim: bool = False):
    """
    Stream a merged PDF for one of the 3 EP prosecution bundles (indices 0–2).

    Session cookies for register.epo.org are established per-request.
    """
    app_no, _, _, docs, session = _fetch_ep_meta_and_docs(number)
    bundles_list = ep_bundles.build_prosecution_bundles(docs)
    three        = ep_bundles.build_three_bundles(bundles_list)

    if bundle_index < 0 or bundle_index >= len(three):
        raise HTTPException(
            status_code=404,
            detail=f"Bundle {bundle_index} not found (total: {len(three)})",
        )
    b = three[bundle_index]

    try:
        pdf = ep_pdf.merge_bundle_pdfs(
            session, b, app_no,
            show_extra=show_extra, show_intclaim=show_intclaim,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    filename = f"{b['filename']}.pdf"
    return StreamingResponse(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/ep/bundles/{number}/all.zip")
def ep_download_all_bundles_zip(number: str,
                                show_extra: bool = False, show_intclaim: bool = False):
    """
    Stream a ZIP of all 3 EP bundle PDFs.

    Matches the CLI's default-mode --download behavior.
    """
    app_no, _, _, docs, session = _fetch_ep_meta_and_docs(number)
    bundles_list = ep_bundles.build_prosecution_bundles(docs)
    three        = ep_bundles.build_three_bundles(bundles_list)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for b in three:
            if not b["documents"]:
                continue
            try:
                pdf = ep_pdf.merge_bundle_pdfs(
                    session, b, app_no,
                    show_extra=show_extra, show_intclaim=show_intclaim,
                )
                zf.writestr(f"{b['filename']}.pdf", pdf.getvalue())
            except ValueError:
                # Bundle had no PDFs under current flags — skip
                pass

    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="EP{app_no}_bundles.zip"'},
    )

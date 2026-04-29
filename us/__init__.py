"""
us — USPTO prosecution-bundle support

Public surface (used by bundles_api.py CLI and bundles_server.py):
    resolver.resolve_application_number(number, force_patent)
    client._get_metadata(app_no) / _get_documents(app_no) / fetch_json(url)
    bundles.build_prosecution_bundles(app_no) / _build_three_bundles(bundles)
    bundles._doc_category(code, bundle_type) / _filter_docs(...)
    pdf._merge_bundle_pdfs(bundle, ...) / _merge_fwclm_pdf(bundles) / get_patent_pdf_url(patent_no)
    manifest._doc_fingerprint(docs) / _load_manifest / _save_manifest / _needs_download
    srch11.is_reachable() / build_granted_claims_pdf(patent_no, grant_date)
    config — constants: HEADERS, GOOGLE_PATENTS_HEADERS, OA_TRIGGER_CODES, etc.
"""

from . import bundles, client, config, manifest, pdf, resolver, srch11

__all__ = [
    "bundles",
    "client",
    "config",
    "manifest",
    "pdf",
    "resolver",
    "srch11",
]

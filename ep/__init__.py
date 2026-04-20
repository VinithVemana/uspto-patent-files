"""
ep — EP (European Patent) prosecution-bundle support

Public surface (used by bundles_api_ep.py CLI and bundles_server.py):
    resolver.resolve(number) -> (app_number, pub_number)
    ops_client.get_publication_biblio(ep_pub) / get_register_biblio(ep_pub)
    ops_client.extract_metadata(pub_biblio, register_biblio)
    register_client.RegisterSession  (warm / list_documents / fetch_pdf)
    bundles.build_prosecution_bundles(documents) / build_three_bundles(bundles)
    pdf.merge_bundle_pdfs(session, bundle, app_number, ...)
    pdf.doc_fingerprint(docs)
    config — user-editable document classification (see module docstring)
"""

from . import auth, bundles, config, ops_client, pdf, register_client, resolver

__all__ = [
    "auth",
    "bundles",
    "config",
    "ops_client",
    "pdf",
    "register_client",
    "resolver",
]

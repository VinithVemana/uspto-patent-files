"""
ep/ops_client.py — EPO Open Patent Services (OPS) API client
============================================================

Thin wrapper around the subset of OPS we need:
  - /register/publication/epodoc/{EPNUM}/biblio
      → gives us the EP application number for any publication number
  - /published-data/publication/epodoc/{EPNUM}/biblio
      → bibliographic metadata (title, status, inventors, applicants, IPC)
  - /published-data/publication/epodoc/{EPNUM}/full-cycle
      → publication-family view (used to surface grant info)

Network layer: 3-attempt exponential backoff on 429/5xx, None on 404/failure.
"""

from __future__ import annotations

import re
import time

import requests

from .auth import ops_auth_headers

OPS_BASE = "https://ops.epo.org/3.2/rest-services"


def _fetch_json(path: str) -> dict | None:
    """
    GET {OPS_BASE}{path} with retry. Returns parsed JSON, or None for 404.
    Auth headers refresh transparently via ep.auth.
    """
    url = f"{OPS_BASE}{path}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=ops_auth_headers(), timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code == 403 and "invalidAccessToken" in r.text.lower():
                # Token was invalidated server-side; force refresh and retry
                from .auth import _cache
                _cache._token = None
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt < 2:
                time.sleep((attempt + 1) * 2)
                continue
            return None
    return None


# ---------------------------------------------------------------------------
# Helpers to defensively walk the OPS JSON (which varies between list / dict)
# ---------------------------------------------------------------------------

def _first(x):
    """Return x[0] if x is a list, else x itself."""
    return x[0] if isinstance(x, list) and x else x


def _txt(node) -> str:
    """Extract the "$" text value from an OPS JSON node (dict / list / None)."""
    node = _first(node)
    if isinstance(node, dict):
        return str(node.get("$", ""))
    return ""


def _iso_date(raw: str) -> str:
    """Convert 'YYYYMMDD' → 'YYYY-MM-DD'. Pass-through if already ISO or empty."""
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_publication_biblio(ep_number: str) -> dict | None:
    """
    Fetch published-data biblio for an EP publication number.
    ep_number examples: 'EP3456789', 'EP2420929' (no kind code).
    """
    return _fetch_json(f"/published-data/publication/epodoc/{ep_number}/biblio")


def get_register_biblio(ep_number: str) -> dict | None:
    """
    Fetch register biblio for an EP publication number.
    Used to discover the application reference (application number).
    """
    return _fetch_json(f"/register/publication/epodoc/{ep_number}/biblio")


def get_register_procedural_steps(ep_number: str) -> dict | None:
    """
    Fetch register procedural-steps (event codes, not PDFs).
    Useful for status display; not the primary source for bundles.
    """
    return _fetch_json(f"/register/publication/epodoc/{ep_number}/procedural-steps")


# ---------------------------------------------------------------------------
# Derived extractions — pull a clean metadata dict out of OPS biblio
# ---------------------------------------------------------------------------

def extract_divisional_parent(register_biblio: dict) -> dict | None:
    """
    Walk register biblio → reg:related-documents → reg:division entries
    looking for a populated <reg:parent-doc>. Returns the parent's identifiers
    if the patent IS a divisional, or None if it's a root (no parent).

    Returned dict shape::

        {"country": "EP", "app_doc_number": "...", "pub_doc_number": "..."}

    Either ``app_doc_number`` or ``pub_doc_number`` may be empty depending on
    what OPS surfaces for the parent. When OPS marks the entry with
    ``@document-id-type``, that takes precedence; otherwise we infer based on
    digit count (≥9 digits → application, else publication).

    Skips:
      - All-empty parent-doc entries (root patents have these as placeholders).
      - Non-EP parents (out of scope).
      - Duplicates: if multiple division events list the same parent, returns
        the first one.
    """
    try:
        reg_doc = _first(register_biblio["ops:world-patent-data"]
                         ["ops:register-search"]["reg:register-documents"]
                         ["reg:register-document"])
        bib = reg_doc.get("reg:bibliographic-data", {})
        related = bib.get("reg:related-documents") or {}
        divisions = related.get("reg:division")
    except (KeyError, TypeError, IndexError):
        return None

    if not divisions:
        return None
    if isinstance(divisions, dict):
        divisions = [divisions]

    seen: set[tuple[str, str]] = set()
    for division in divisions:
        if not isinstance(division, dict):
            continue
        relation = division.get("reg:relation")
        if not isinstance(relation, dict):
            continue
        parent_doc = relation.get("reg:parent-doc")
        if not isinstance(parent_doc, dict):
            continue
        doc_ids = parent_doc.get("reg:document-id")
        if isinstance(doc_ids, dict):
            doc_ids = [doc_ids]
        if not isinstance(doc_ids, list):
            continue

        app_num, pub_num, country = "", "", ""
        for did in doc_ids:
            if not isinstance(did, dict):
                continue
            c = _txt(did.get("reg:country")).strip()
            n = _txt(did.get("reg:doc-number")).strip()
            if not c or not n:
                continue  # root patents have empty placeholders
            country = c
            id_type = did.get("@document-id-type", "")
            if id_type == "application number":
                app_num = n
            elif id_type == "publication number":
                pub_num = n
            elif len(n) >= 9:
                app_num = n
            else:
                pub_num = n

        if not country or country.upper() != "EP":
            continue
        key = (country, app_num or pub_num)
        if key in seen:
            continue
        seen.add(key)
        return {"country": country, "app_doc_number": app_num, "pub_doc_number": pub_num}

    return None


def extract_divisional_children(register_biblio: dict) -> list[dict]:
    """
    Walk register biblio → reg:related-documents → reg:division entries
    looking for ones where THIS patent is the PARENT of the division (i.e.,
    <reg:parent-doc> is an empty placeholder). Returns the list of child
    identifiers.

    Returned list contains dicts like::

        {"country": "EP", "app_doc_number": "...", "pub_doc_number": "..."}

    Either id may be empty depending on what OPS surfaces. Duplicates
    (same child appearing in multiple division entries) are filtered.

    Skips entries where <reg:parent-doc> is populated (those are upward
    relationships handled by `extract_divisional_parent`).
    """
    try:
        reg_doc = _first(register_biblio["ops:world-patent-data"]
                         ["ops:register-search"]["reg:register-documents"]
                         ["reg:register-document"])
        bib = reg_doc.get("reg:bibliographic-data", {})
        related = bib.get("reg:related-documents") or {}
        divisions = related.get("reg:division")
    except (KeyError, TypeError, IndexError):
        return []

    if not divisions:
        return []
    if isinstance(divisions, dict):
        divisions = [divisions]

    children: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for division in divisions:
        if not isinstance(division, dict):
            continue
        relation = division.get("reg:relation")
        if not isinstance(relation, dict):
            continue

        # If parent-doc is populated, the current patent is the CHILD of this
        # division event — skip (extract_divisional_parent handles that).
        parent_doc = relation.get("reg:parent-doc", {})
        if isinstance(parent_doc, dict):
            pdid = parent_doc.get("reg:document-id")
            # Handle both dict and list-of-dict shapes
            for pd in (pdid if isinstance(pdid, list) else [pdid] if pdid else []):
                if not isinstance(pd, dict):
                    continue
                pc = _txt(pd.get("reg:country")).strip()
                pn = _txt(pd.get("reg:doc-number")).strip()
                if pc and pn:
                    # parent-doc is populated → skip this entry (upward relation)
                    parent_doc = None  # sentinel: treat as skip
                    break
            if parent_doc is None:
                continue

        child_doc = relation.get("reg:child-doc")
        if not isinstance(child_doc, dict):
            continue
        doc_ids = child_doc.get("reg:document-id")
        if isinstance(doc_ids, dict):
            doc_ids = [doc_ids]
        if not isinstance(doc_ids, list):
            continue

        app_num, pub_num, country = "", "", ""
        for did in doc_ids:
            if not isinstance(did, dict):
                continue
            c = _txt(did.get("reg:country")).strip()
            n = _txt(did.get("reg:doc-number")).strip()
            if not c or not n:
                continue
            country = c
            id_type = did.get("@document-id-type", "")
            if id_type == "application number":
                app_num = n
            elif id_type == "publication number":
                pub_num = n
            elif len(n) >= 9:
                app_num = n
            else:
                pub_num = n

        if not country or country.upper() != "EP":
            continue
        key = (country, app_num or pub_num)
        if key in seen:
            continue
        seen.add(key)
        children.append({"country": country, "app_doc_number": app_num, "pub_doc_number": pub_num})

    return children


def extract_application_number(register_biblio: dict) -> str | None:
    """
    Walk register biblio → bibliographic-data → application-reference → EP doc-number.
    Returns a plain digit string like '10173239' (without 'EP' prefix).
    """
    try:
        reg_doc = (register_biblio["ops:world-patent-data"]
                   ["ops:register-search"]["reg:register-documents"]
                   ["reg:register-document"])
        reg_doc = _first(reg_doc)
        app_refs = reg_doc["reg:bibliographic-data"]["reg:application-reference"]
        # Can be a dict or list of dicts (multiple languages / formats)
        if isinstance(app_refs, list):
            # Prefer the one whose country is EP
            for ref in app_refs:
                doc_id = ref.get("reg:document-id", {})
                if _txt(doc_id.get("reg:country")) == "EP":
                    return _txt(doc_id.get("reg:doc-number")) or None
            app_refs = app_refs[0]
        doc_id = app_refs.get("reg:document-id", {})
        if _txt(doc_id.get("reg:country")) == "EP":
            return _txt(doc_id.get("reg:doc-number")) or None
    except (KeyError, TypeError, IndexError):
        pass
    return None


def extract_metadata(pub_biblio: dict, register_biblio: dict | None = None) -> dict:
    """
    Return a normalized metadata dict from published-data biblio.

    Keys mirror the USPTO metadata structure where possible:
        application_number, publication_number, patent_number, kind_code,
        title, status, filing_date, publication_date, grant_date,
        inventors (list of {name, location}), applicants (list of name strings),
        ipc_codes (list of strings), language.
    """
    out: dict = {
        "application_number": None,
        "publication_number": None,
        "patent_number": None,
        "kind_code": None,
        "title": "N/A",
        "status": "N/A",
        "filing_date": "",
        "publication_date": "",
        "grant_date": None,
        "inventors": [],
        "applicants": [],
        "ipc_codes": [],
        "language": "",
    }
    try:
        exdoc = (pub_biblio["ops:world-patent-data"]
                 ["exchange-documents"]["exchange-document"])
        exdoc = _first(exdoc)
    except (KeyError, TypeError, IndexError):
        return out

    bib = exdoc.get("bibliographic-data", {})

    # --- Title (prefer English) ---
    titles = bib.get("invention-title", [])
    if isinstance(titles, dict): titles = [titles]
    en_title = next((t for t in titles if t.get("@lang") == "en"), None)
    chosen = en_title or (_first(titles) if titles else None)
    if isinstance(chosen, dict):
        out["title"] = chosen.get("$", "N/A")
        out["language"] = chosen.get("@lang", "")

    # --- Publication reference (number + kind + date) ---
    # OPS epodoc format ships the country prefix inside doc-number (e.g.
    # "EP2420929"). Strip it so downstream code can uniformly `f"EP{num}"`.
    pub_refs = bib.get("publication-reference", {}).get("document-id", [])
    if isinstance(pub_refs, dict): pub_refs = [pub_refs]
    for pref in pub_refs:
        if pref.get("@document-id-type") == "epodoc":
            raw = _txt(pref.get("doc-number"))
            out["publication_number"] = re.sub(r"^EP", "", raw) if raw else None
            out["publication_date"]   = _iso_date(_txt(pref.get("date")))
        elif pref.get("@document-id-type") == "docdb":
            out["kind_code"] = _txt(pref.get("kind"))

    # --- Application reference (number) ---
    app_refs = bib.get("application-reference", {}).get("document-id", [])
    if isinstance(app_refs, dict): app_refs = [app_refs]
    for aref in app_refs:
        if aref.get("@document-id-type") == "epodoc":
            raw = _txt(aref.get("doc-number"))
            out["application_number"] = re.sub(r"^EP", "", raw) if raw else None
            if not out["filing_date"]:
                out["filing_date"] = _iso_date(_txt(aref.get("date")))

    # --- Override application_number with register if we have it ---
    if register_biblio is not None:
        reg_app = extract_application_number(register_biblio)
        if reg_app:
            out["application_number"] = reg_app

    # --- Inventors ---
    parties = bib.get("parties", {})
    inv_node = parties.get("inventors", {}).get("inventor", [])
    if isinstance(inv_node, dict): inv_node = [inv_node]
    for inv in inv_node:
        if inv.get("@data-format") == "epodoc":
            name = _txt(inv.get("inventor-name", {}).get("name"))
            if name:
                out["inventors"].append({"name": name, "location": ""})

    # --- Applicants ---
    app_node = parties.get("applicants", {}).get("applicant", [])
    if isinstance(app_node, dict): app_node = [app_node]
    for app in app_node:
        if app.get("@data-format") == "epodoc":
            name = _txt(app.get("applicant-name", {}).get("name"))
            if name:
                out["applicants"].append(name)

    # --- IPC codes ---
    ipc = bib.get("classifications-ipcr", {}).get("classification-ipcr", [])
    if isinstance(ipc, dict): ipc = [ipc]
    for c in ipc:
        t = _txt(c.get("text"))
        if t:
            # IPC codes from OPS have trailing whitespace; first token is the code
            out["ipc_codes"].append(t.strip().split()[0])

    # --- Status (from register if available — more authoritative) ---
    if register_biblio is not None:
        try:
            reg_doc = _first(register_biblio["ops:world-patent-data"]
                             ["ops:register-search"]["reg:register-documents"]
                             ["reg:register-document"])
            status = reg_doc.get("reg:bibliographic-data", {}).get("@status")
            if status:
                out["status"] = status
        except (KeyError, TypeError, IndexError):
            pass

    # --- Grant date / patent number: EP publication B1 kind means granted ---
    if out.get("kind_code", "").upper().startswith("B"):
        out["patent_number"] = out["publication_number"]
        out["grant_date"] = out["publication_date"] or None

    return out

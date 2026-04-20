"""
ep/resolver.py — Input number resolver for EP patents
=====================================================

Accepts any common EP-patent identifier and resolves it to
(application_number, publication_number) tuple, using OPS as the
authoritative source.

Supported input formats
-----------------------
Application number (8 digits, year-prefixed):
    10173239             → app 10173239
    10173239.4           → check digit stripped → app 10173239
    EP10173239           → app 10173239
    EP10173239.4         → app 10173239

EP publication number (7 digits, optionally with kind code):
    EP3456789            → publication → app via OPS register biblio
    EP3456789A1          → kind code stripped → publication → app
    EP3456789B1          → kind code stripped → publication → app
    3456789              → ambiguous, tries as publication

PCT / International number:
    WO2015077217         → resolve PCT → EP app via family lookup
    WO2015/077217        → slash stripped
    PCT/US2020/012345    → tries as PCT publication equivalent

Resolution strategy
-------------------
1. Strip decorations (slashes, commas, spaces, check digits).
2. If input looks like an EP application number (8 digits starting with a
   2-digit year code), use it directly; verify via OPS register biblio.
3. If input looks like an EP publication number (7 digits), OPS register
   biblio returns the app ref.
4. If input is WO/PCT, look up the EP family member via OPS.

Returns ValueError when the input cannot be resolved.
"""

from __future__ import annotations

import re

from . import ops_client


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_KIND_CODE_RE = re.compile(r"[A-Z]\d?$", re.IGNORECASE)


def _strip(s: str) -> str:
    """Remove whitespace, commas, slashes."""
    return re.sub(r"[\s,/]", "", s.strip())


def _strip_country_prefix(s: str) -> tuple[str, str]:
    """Return (country_prefix, rest). country_prefix ∈ {'EP', 'WO', 'PCT', ''}."""
    s = s.strip()
    m = re.match(r"(?i)^(EP|WO|PCT)", s)
    if m:
        return m.group(1).upper(), s[m.end():]
    return "", s


def _strip_kind_code(s: str) -> str:
    """Remove trailing kind codes like A1, A2, A9, B1, B2, U1."""
    return _KIND_CODE_RE.sub("", s).rstrip()


def _strip_check_digit(s: str) -> str:
    """EP application numbers sometimes come with a dot + check digit: 10173239.4"""
    return re.sub(r"\.\d+$", "", s)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _is_ep_application_number(digits: str) -> bool:
    """
    EP application numbers are 8 digits: 2-digit year (YY) + 6-digit serial.
    E.g. 10173239 = filed 2010, serial 173239.
    """
    return bool(re.fullmatch(r"\d{8}", digits))


def _is_ep_publication_number(digits: str) -> bool:
    """
    EP publication numbers are 7 digits for pre-2003 (EP1234567) and
    7 digits for post-2003 (EP1234567 through EP9999999). Always 7 digits.
    """
    return bool(re.fullmatch(r"\d{7}", digits))


def _is_wo_publication_number(digits: str) -> bool:
    """
    WO publication: YYYY + 6-digit serial = 10 digits (post-2004 format).
    Legacy: YYYY + 5 digits = 9 digits.
    """
    return bool(re.fullmatch(r"\d{9,10}", digits))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(number: str) -> tuple[str, str | None]:
    """
    Resolve an input number to (application_number, publication_number).

    application_number  — 8-digit string without 'EP' prefix (e.g. '10173239')
    publication_number  — 7-digit string without 'EP' prefix, or None if
                          we could only recover the app number.

    Raises ValueError if the input cannot be resolved.
    """
    raw = number
    s = _strip(number)
    country, rest = _strip_country_prefix(s)
    rest = _strip_kind_code(rest)
    rest = _strip_check_digit(rest)
    digits = re.sub(r"[^\d]", "", rest)

    if not digits:
        raise ValueError(f"No digits found in input '{raw}'")

    # --- Route 1: WO/PCT number → look up EP family member ---
    if country in ("WO", "PCT") or _is_wo_publication_number(digits):
        app_num = _resolve_wo_to_ep_app(digits)
        if app_num:
            # Find EP publication number for convenience
            pub_num = _find_ep_publication_for_app(app_num)
            return app_num, pub_num
        raise ValueError(
            f"Could not resolve PCT/WO number '{raw}' to an EP application. "
            f"The application may not have entered the European regional phase."
        )

    # --- Route 2: EP publication number (7 digits) ---
    if country == "EP" and _is_ep_publication_number(digits) or _is_ep_publication_number(digits):
        # country was either explicit EP or inferred from digit count
        app_num = _resolve_ep_publication_to_app(digits)
        if app_num:
            return app_num, digits
        # Fall through — maybe the digits are actually an app number

    # --- Route 3: EP application number (8 digits) ---
    if _is_ep_application_number(digits):
        # Verify by fetching register biblio using any EP publication related to this app.
        # We don't have a direct /register/application/epodoc endpoint, so we try the
        # doclist page (via resolver test). For now, return the digits — the caller
        # will use RegisterSession which will surface any 404/invalid number.
        return digits, None

    raise ValueError(
        f"Cannot recognise '{raw}' as an EP application, EP publication, or WO/PCT number"
    )


# ---------------------------------------------------------------------------
# Internals — use OPS to map publication → application
# ---------------------------------------------------------------------------

def _resolve_ep_publication_to_app(pub_digits: str) -> str | None:
    """Use OPS register biblio to find the application number for EP{pub_digits}."""
    biblio = ops_client.get_register_biblio(f"EP{pub_digits}")
    if not biblio:
        return None
    return ops_client.extract_application_number(biblio)


def _resolve_wo_to_ep_app(wo_digits: str) -> str | None:
    """
    Resolve a WO/PCT publication number → EP application number.

    Strategy (each is best-effort; first hit wins):
      1. OPS /family/publication/docdb/WO{digits}/biblio  — walk family-members
      2. OPS /family/publication/epodoc/WO{digits}/biblio — epodoc variant
      3. OPS search: q=pa=WO{digits} restricted to EP publications

    Returns None when no EP regional phase entry can be located.
    """
    for path in (
        f"/family/publication/docdb/WO{wo_digits}/biblio",
        f"/family/publication/epodoc/WO{wo_digits}/biblio",
    ):
        data = ops_client._fetch_json(path)
        if not data:
            continue
        try:
            members = (data["ops:world-patent-data"]["ops:patent-family"]
                       ["ops:family-member"])
            if isinstance(members, dict):
                members = [members]
        except (KeyError, TypeError):
            continue

        for m in members:
            try:
                app_ref = m["application-reference"]["document-id"]
                if isinstance(app_ref, dict): app_ref = [app_ref]
                for ref in app_ref:
                    if (ref.get("@document-id-type") in ("epodoc", "docdb")
                            and ops_client._txt(ref.get("country")) == "EP"):
                        raw = ops_client._txt(ref.get("doc-number"))
                        return re.sub(r"^EP", "", raw) or None
            except (KeyError, TypeError):
                continue

    # Fallback: search for EP publications whose application shares the PCT reference
    data = ops_client._fetch_json(
        f"/published-data/search?q=pn%3DWO{wo_digits}%20AND%20pn%3DEP&Range=1-5"
    )
    if not data:
        return None
    try:
        pubs = (data["ops:world-patent-data"]["ops:biblio-search"]
                ["ops:search-result"]["ops:publication-reference"])
        if isinstance(pubs, dict): pubs = [pubs]
        for pub in pubs:
            docs = pub.get("document-id", [])
            if isinstance(docs, dict): docs = [docs]
            for d in docs:
                if (d.get("@document-id-type") == "epodoc"
                        and ops_client._txt(d.get("country")) == "EP"):
                    # Got EP publication — now resolve to app via register biblio
                    epnum = ops_client._txt(d.get("doc-number"))
                    epnum = re.sub(r"^EP", "", epnum)
                    if epnum:
                        return _resolve_ep_publication_to_app(epnum)
    except (KeyError, TypeError, IndexError):
        pass
    return None


def _find_ep_publication_for_app(app_digits: str) -> str | None:
    """
    Given an EP application number, try to find its earliest EP publication.
    OPS doesn't have a direct reverse lookup, but published-data search by
    application number works via the 'ap' field.
    """
    data = ops_client._fetch_json(
        f"/published-data/search?q=ap%3D{app_digits}&Range=1-1"
    )
    if not data:
        return None
    try:
        results = (data["ops:world-patent-data"]["ops:biblio-search"]
                   ["ops:search-result"]["ops:publication-reference"])
        if isinstance(results, dict): results = [results]
        for pub in results:
            doc_ids = pub.get("document-id", [])
            if isinstance(doc_ids, dict): doc_ids = [doc_ids]
            for d in doc_ids:
                if d.get("@document-id-type") == "epodoc":
                    num = ops_client._txt(d.get("doc-number"))
                    # Strip any 'EP' prefix
                    return re.sub(r"^EP", "", num) or None
    except (KeyError, TypeError, IndexError):
        pass
    return None

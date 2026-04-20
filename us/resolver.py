"""
us/resolver.py — USPTO input normalization and application-number resolution
"""

import re

from .config import BASE_API
from .client import fetch_json, _get_metadata


def _is_publication_number(s: str) -> bool:
    """Return True if the string looks like a pre-grant publication (kind code A1/A2/A9)."""
    return bool(re.search(r"[Aa][129]\s*$", s.strip()))


def _extract_patent_digits(number: str) -> str:
    """
    Normalize a patent grant number string to digits only.

    Handles:
      - Formatting separators (commas, slashes, spaces): '11,973,593' → '11973593'
      - 'US' prefix: 'US10902286' → '10902286'
      - Kind codes at end: 'US11973593B2' → '11973593', '10902286B1' → '10902286'
    """
    s = re.sub(r"[,/\s]", "", number.strip())
    s = re.sub(r"(?i)^US", "", s)       # strip US prefix
    s = re.sub(r"[A-Za-z]\d*$", "", s)  # strip trailing kind code (B2, B1, A1 …)
    return re.sub(r"[^\d]", "", s)


def resolve_patent_to_application(patent_digits: str) -> str | None:
    """
    Given a patent grant number as digits only (e.g. '10902286'),
    return the USPTO application number (e.g. '16123456').
    Returns None if not found.

    Caller must strip the 'US' prefix and kind codes before calling —
    use _extract_patent_digits() for that.
    """
    data = fetch_json(
        f"{BASE_API}/search"
        f"?q=applicationMetaData.patentNumber:{patent_digits}"
        f"&fields=applicationNumberText,applicationMetaData.patentNumber"
        f"&limit=1"
    )
    if not data:
        return None
    bag = data.get("patentFileWrapperDataBag", [])
    if not bag:
        return None
    return bag[0].get("applicationNumberText")


def resolve_publication_to_application(pub_number: str) -> str | None:
    """
    Given a pre-grant publication number in full form (e.g. 'US20210367709A1'),
    return the USPTO application number.
    Returns None if not found.

    The earliestPublicationNumber field stores the full string including 'US'
    prefix and kind code — stripping them produces a 404.
    """
    data = fetch_json(
        f"{BASE_API}/search"
        f"?q=applicationMetaData.earliestPublicationNumber:{pub_number}"
        f"&fields=applicationNumberText,applicationMetaData.earliestPublicationNumber"
        f"&limit=1"
    )
    if not data:
        return None
    bag = data.get("patentFileWrapperDataBag", [])
    if not bag:
        return None
    return bag[0].get("applicationNumberText")


def resolve_application_number(number: str, force_patent: bool = False) -> str:
    """
    Accept a USPTO application number or patent grant number in any common format.

    Input normalization (always applied first):
      - Commas, slashes, spaces stripped  →  '16/123,456' becomes '16123456'

    Resolution order:
      1. Input contains 'US' prefix with publication kind code (A1/A2/A9,
         e.g. 'US20210367709A1') → publication→app lookup via earliestPublicationNumber.
      2. Input contains 'US' prefix without publication kind code
         (e.g. 'US10902286', 'US11973593B2') → patent→app lookup via patentNumber.
      3. force_patent=True → treat bare digits as a patent number directly.
      4. Bare digits only → try as application number first; fall back to patent lookup.

    Raises ValueError when the input cannot be resolved.
    """
    s = re.sub(r"[,/\s]", "", number.strip())

    if re.match(r"(?i)^US", s):
        if _is_publication_number(s):
            app_no = resolve_publication_to_application(s)
        else:
            app_no = resolve_patent_to_application(_extract_patent_digits(s))
        if not app_no:
            raise ValueError(
                f"Could not resolve patent number '{number}' to a USPTO application number"
            )
        return app_no

    digits = re.sub(r"[^\d]", "", s)

    if force_patent:
        app_no = resolve_patent_to_application(digits)
        if not app_no:
            raise ValueError(
                f"Could not resolve '{number}' as a patent number to a USPTO application number"
            )
        return app_no

    if _get_metadata(digits):
        return digits
    app_no = resolve_patent_to_application(digits)
    if app_no:
        return app_no

    return digits

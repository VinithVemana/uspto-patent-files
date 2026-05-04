"""
us/pcs_api.py — Granted-claims source: Dolcera PCS proxy (dev2.dolcera.net)

Primary granted-claims source. Queries the PCS proxy `service2/search`
endpoint, which returns the same USPTO claim XML markup that srch11
returns, parsed via the shared `srch11.parse_claims` helper and rendered
with `srch11.render_claims_pdf`.

Source policy in `bundles_api._build_granted_claims_pdf`:
    1. pcs_api  (this module)               — primary
    2. srch11   (us/srch11.py)              — fallback when pcs unreachable
                                              / no match / parse error
    3. USPTO    (us.pdf._merge_bundle_pdfs) — last resort

Configuration (env, optional)
-----------------------------
- ``PCS_API_BASE_URL``  — default ``https://dev2.dolcera.net/pcs_api/api/proxy/service2``
- ``PCS_API_KEY``       — default empty. When empty, ``is_reachable()``
                          returns False and pcs_api is silently skipped
                          (callers fall through to srch11).
- ``PCS_API_PORT``      — default ``8000`` (proxy backend port).

Query schema
------------
Granted patents are indexed under publication numbers with a kind-code
suffix. We query for both common utility-grant kind codes in one OR
clause; the proxy returns ``rows=2`` so either variant matches:

    pn:"US-{patent_no}-B2" OR pn:"US-{patent_no}-B1"

The first response doc's ``clm[0]`` is the granted-publication claim XML.
Reissue (E1/E2), design (S1), and plant (P1–P3) kinds are not in this
query — those fall through to srch11.
"""

import io
import json
import os
import socket
import sys
from urllib.parse import urlparse

import requests

from . import srch11


PCS_API_BASE_URL = os.environ.get(
    "PCS_API_BASE_URL",
    "https://dev2.dolcera.net/pcs_api/api/proxy/service2",
)
PCS_API_KEY  = os.environ.get("PCS_API_KEY", "")
PCS_API_PORT = int(os.environ.get("PCS_API_PORT", "8000"))

HTTP_TIMEOUT = 30
TCP_TIMEOUT  = 2.0

_reachable_cache: bool | None = None


def _host_port() -> tuple[str, int]:
    """Parse host + port for the TCP probe from PCS_API_BASE_URL."""
    u = urlparse(PCS_API_BASE_URL)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port


def is_reachable() -> bool:
    """
    Decide whether pcs_api should be tried for granted claims.

    Returns False when ``PCS_API_KEY`` is empty (opt-out) or the host is
    unreachable on a 2s TCP probe. Cached for the process so the probe
    runs at most once per run.
    """
    global _reachable_cache
    if _reachable_cache is not None:
        return _reachable_cache

    if not PCS_API_KEY:
        _reachable_cache = False
        print(f"  [pcs_api] PCS_API_KEY not set — skipping pcs_api, "
              f"will use srch11 / USPTO for granted claims", file=sys.stderr)
        return _reachable_cache

    host, port = _host_port()
    if not host:
        _reachable_cache = False
        print(f"  [pcs_api] invalid PCS_API_BASE_URL={PCS_API_BASE_URL!r} — "
              f"skipping pcs_api", file=sys.stderr)
        return _reachable_cache

    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            _reachable_cache = True
            print(f"  [pcs_api] TCP probe to {host}:{port} succeeded — "
                  f"will use pcs_api for granted claims", file=sys.stderr)
    except (OSError, socket.timeout) as exc:
        _reachable_cache = False
        print(f"  [pcs_api] TCP probe to {host}:{port} failed ({exc}) — "
              f"will fall back to srch11 / USPTO for granted claims",
              file=sys.stderr)
    return _reachable_cache


def _unwrap(data):
    """PCS proxy responses wrap the payload in a `data` key."""
    return data.get("data", data) if isinstance(data, dict) else data


def fetch_claims_xml(patent_number: str) -> str | None:
    """
    Query the PCS proxy for the granted-publication claim XML.

    Returns the first ``clm[0]`` from the first matching doc, or None when:
      - the proxy returns no docs
      - the response is malformed / missing the field
      - any network or HTTP error occurs
    """
    query = (
        f'pn:"US-{patent_number}-B2" OR '
        f'pn:"US-{patent_number}-B1"'
    )
    payload = {
        "api_key":     PCS_API_KEY,
        "q":           query,
        "fields":      ["clm"],
        "rows":        2,
        "filters":     [],
        "cursorMark":  "*",
        "sort":        [],
        "extraParams": {},
    }

    is_proxy = "proxy" in PCS_API_BASE_URL
    print(f"    [pcs_api] querying for US{patent_number} ...", file=sys.stderr)

    try:
        if is_proxy:
            resp = requests.post(
                f"{PCS_API_BASE_URL}/search",
                data={"port": PCS_API_PORT, "input": json.dumps(payload)},
                timeout=HTTP_TIMEOUT,
            )
        else:
            resp = requests.post(
                f"{PCS_API_BASE_URL}/search",
                json=payload,
                headers={"X-API-Key": PCS_API_KEY},
                timeout=HTTP_TIMEOUT,
            )
        resp.raise_for_status()
        result = _unwrap(resp.json())
    except (requests.RequestException, ValueError) as exc:
        print(f"    [pcs_api] query failed: {exc}", file=sys.stderr)
        return None

    if not isinstance(result, dict):
        print(f"    [pcs_api] unexpected response shape: {type(result).__name__}",
              file=sys.stderr)
        return None

    docs = result.get("docs", [])
    print(f"    [pcs_api] docs returned: {len(docs)}", file=sys.stderr)
    if not docs:
        return None

    clm = docs[0].get("clm")
    if not clm:
        print(f"    [pcs_api] doc missing 'clm' field "
              f"(keys={list(docs[0].keys())})", file=sys.stderr)
        return None

    first = clm[0] if isinstance(clm, list) else clm
    if not isinstance(first, str) or not first.strip():
        print(f"    [pcs_api] clm[0] is empty/non-string", file=sys.stderr)
        return None

    list_size = len(clm) if isinstance(clm, list) else 1
    print(f"    [pcs_api] clm xml fetched ({len(first):,} chars, "
          f"clm list has {list_size} elements — using first)", file=sys.stderr)
    return first


def build_granted_claims_pdf(
    patent_number: str, grant_date: str | None = None
) -> tuple[io.BytesIO | None, str]:
    """
    End-to-end: reachability → PCS fetch → parse → render.

    Reuses ``srch11.parse_claims`` and ``srch11.render_claims_pdf`` since
    the proxy returns the same USPTO ``<claims>`` XML markup.

    Returns ``(pdf_buf, reason)``. On failure ``pdf_buf`` is None and
    ``reason`` explains why so the caller can log it and fall back to
    srch11 / USPTO.
    """
    if not is_reachable():
        return None, "pcs_api unreachable"
    xml = fetch_claims_xml(patent_number)
    if xml is None:
        return None, "no pcs_api match"
    try:
        claims, statement = srch11.parse_claims(xml)
    except Exception as exc:
        return None, f"XML parse error: {exc}"
    if not claims:
        return None, "parsed 0 claims"

    sub_blocks = sum(1 for c in claims for d, _ in c["blocks"] if d > 0)
    first_lead = next(
        (t for d, t in claims[0]["blocks"] if d == 0), claims[0]["blocks"][0][1]
    )
    print(f"    [pcs_api] parsed {len(claims)} claim(s) "
          f"({sub_blocks} sub-elements, statement={'yes' if statement else 'no'}) "
          f"— claim 1 lead: {first_lead[:80]}...", file=sys.stderr)

    try:
        buf = srch11.render_claims_pdf(claims, statement, patent_number, grant_date)
        print(f"    [pcs_api] rendered PDF ({len(buf.getvalue()):,} bytes)",
              file=sys.stderr)
        return buf, "ok"
    except Exception as exc:
        return None, f"render error: {exc}"

"""
us/pcs_api.py — Granted-claims source: Dolcera PCS proxy (dev2.dolcera.net)

Primary granted-claims source for both US and EP. Queries the PCS proxy
`service2/search` endpoint, which returns USPTO-flavoured claim XML for
both jurisdictions (Dolcera stores both under the same `<claims>` schema),
parsed via the shared `srch11.parse_claims` helper and rendered with
`srch11.render_claims_pdf`.

Source policy:
  US — `bundles_api._build_granted_claims_pdf`:
    1. pcs_api  (this module)               — primary
    2. srch11   (us/srch11.py)              — fallback when pcs unreachable
                                              / no match / parse error
    3. USPTO    (us.pdf._merge_bundle_pdfs) — last resort
  EP — `bundles_api_ep._build_granted_claims_pdf`:
    1. pcs_api  (this module)               — primary
    2. EPO Reg. (ep.pdf.merge_bundle_pdfs)  — fallback

Configuration (env, optional)
-----------------------------
- ``PCS_API_BASE_URL``  — default ``https://dev2.dolcera.net/pcs_api/api/proxy/service2``
- ``PCS_API_KEY``       — default empty. When empty, ``is_reachable()``
                          returns False and pcs_api is silently skipped
                          (callers fall through to the next source).
- ``PCS_API_PORT``      — default ``8000`` (proxy backend port).

Query schema
------------
Granted patents are indexed under publication numbers with a kind-code
suffix.

  US — both common utility-grant kind codes in one OR clause:
       ``pn:"US-{patent_no}-B2" OR pn:"US-{patent_no}-B1"``
       Reissue (E1/E2), design (S1), and plant (P1–P3) kinds fall through.

  EP — exact kind code from OPS biblio (B1 / B2 / B3):
       ``pn:"EP-{pub_no}-{kind_code}"``
       When OPS doesn't return a kind code, the EP path is skipped.

The first response doc's ``clm[0]`` is the granted-publication claim XML.
"""

from __future__ import annotations

import io
import json
import os
import re
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
              f"will use fallback source for granted claims", file=sys.stderr)
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
              f"will fall back for granted claims", file=sys.stderr)
    return _reachable_cache


def _unwrap(data):
    """PCS proxy responses wrap the payload in a `data` key."""
    return data.get("data", data) if isinstance(data, dict) else data


def _post_pcs_query(query: str, log_label: str) -> list[str] | None:
    """
    POST a Solr query to the PCS proxy and return the first doc's
    ``clm`` list (string entries only) or None.

    Shared by the US and EP fetch wrappers — only the Solr ``q`` string
    and the log label differ between jurisdictions. Callers pick which
    entry of the returned list they want (US: ``clm[0]``; EP: filter by
    ``lang="EN"`` since PCS stores DE/EN/FR variants).
    """
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
    print(f"    [pcs_api] querying for {log_label} ...", file=sys.stderr)

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

    items = clm if isinstance(clm, list) else [clm]
    strings = [s for s in items if isinstance(s, str) and s.strip()]
    if not strings:
        print(f"    [pcs_api] clm list contained no usable string entries",
              file=sys.stderr)
        return None

    print(f"    [pcs_api] clm list has {len(items)} entries "
          f"({len(strings)} non-empty strings)", file=sys.stderr)
    return strings


_LANG_ATTR_RE = re.compile(r'lang="([A-Z]{2})"', re.IGNORECASE)


def _pick_claims_xml(clm_strings: list[str], prefer_lang: str | None) -> str | None:
    """
    Pick the desired ``<claims>``-rooted entry from the clm list.

    EP claim XML is returned in multiple languages (DE/EN/FR), each as a
    standalone ``<claims lang="XX">`` document, alongside individual
    ``<claim>`` fragments. When ``prefer_lang`` is set we look for the
    first entry that (a) starts with ``<claims`` and (b) has the matching
    ``lang`` attribute. Fallback is the first entry. US callers pass
    ``prefer_lang=None`` to get ``clm[0]`` (legacy behaviour).
    """
    if not prefer_lang:
        return clm_strings[0]

    want = prefer_lang.upper()
    for s in clm_strings:
        head = s[:300]
        if "<claims" not in head:
            continue
        m = _LANG_ATTR_RE.search(head)
        if m and m.group(1).upper() == want:
            print(f"    [pcs_api] picked entry with lang={want}", file=sys.stderr)
            return s

    print(f"    [pcs_api] no clm entry with lang={want} — using first entry",
          file=sys.stderr)
    return clm_strings[0]


def fetch_claims_xml(patent_number: str) -> str | None:
    """US granted-publication claim XML via PCS proxy. Returns None on miss."""
    query = (
        f'pn:"US-{patent_number}-B2" OR '
        f'pn:"US-{patent_number}-B1"'
    )
    clm = _post_pcs_query(query, f"US{patent_number}")
    if not clm:
        return None
    return _pick_claims_xml(clm, prefer_lang=None)


def fetch_claims_xml_ep(pub_no: str, kind_code: str | None = None) -> tuple[str | None, str | None]:
    """
    EP granted-publication claim XML via PCS proxy.

    Tries B2 → B1 → B3 regardless of ``kind_code`` from OPS biblio (which is
    often "A1" — the pre-grant publication). When ``kind_code`` is a B-series
    code it is tried first. Returns ``(xml, resolved_kind_code)`` or
    ``(None, None)`` on miss. PCS stores EP claims in DE/EN/FR — picks EN.
    """
    # Build the order: hint first (if B-series), then remaining B variants.
    hint = kind_code.upper() if kind_code and kind_code.upper().startswith("B") else None
    order = [hint] if hint else []
    for kc in ("B2", "B1", "B3"):
        if kc != hint:
            order.append(kc)

    for kc in order:
        query = f'pn:"EP-{pub_no}-{kc}"'
        clm = _post_pcs_query(query, f"EP{pub_no}{kc}")
        if clm:
            xml = _pick_claims_xml(clm, prefer_lang="EN")
            if xml:
                return xml, kc
    return None, None


def _render_from_xml(
    xml: str, display_pn: str, grant_date: str | None
) -> tuple[io.BytesIO | None, str]:
    """Parse claim XML and render to PDF — shared by US and EP builders."""
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
        buf = srch11.render_claims_pdf(claims, statement, display_pn, grant_date)
        print(f"    [pcs_api] rendered PDF ({len(buf.getvalue()):,} bytes)",
              file=sys.stderr)
        return buf, "ok"
    except Exception as exc:
        return None, f"render error: {exc}"


def build_granted_claims_pdf(
    patent_number: str, grant_date: str | None = None
) -> tuple[io.BytesIO | None, str]:
    """
    US end-to-end: reachability → PCS fetch → parse → render.

    Returns ``(pdf_buf, reason)``. On failure ``pdf_buf`` is None and
    ``reason`` explains why so the caller can log it and fall back to
    srch11 / USPTO.
    """
    if not is_reachable():
        return None, "pcs_api unreachable"
    xml = fetch_claims_xml(patent_number)
    if xml is None:
        return None, "no pcs_api match"
    return _render_from_xml(xml, patent_number, grant_date)


def build_granted_claims_pdf_ep(
    pub_no: str, kind_code: str | None = None, grant_date: str | None = None
) -> tuple[io.BytesIO | None, str]:
    """
    EP end-to-end: reachability → PCS fetch (B2/B1/B3) → parse → render.

    ``kind_code`` is optional — when OPS biblio returns "A1" (pre-grant) or
    nothing, the function tries B2 → B1 → B3 automatically. When it IS a
    B-series code it is tried first. Returns ``(pdf_buf, reason)``; on
    failure ``pdf_buf`` is None and the caller falls back to EPO merge.
    """
    if not is_reachable():
        return None, "pcs_api unreachable"
    xml, resolved_kc = fetch_claims_xml_ep(pub_no, kind_code)
    if xml is None:
        return None, "no pcs_api match"
    return _render_from_xml(xml, f"EP{pub_no}{resolved_kc}", grant_date)

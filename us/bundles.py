"""
us/bundles.py — Prosecution bundle builder and 3-bundle collapse
"""

from datetime import datetime

from .config import (
    OA_TRIGGER_CODES,
    OA_SUPPORTING_CODES,
    RESPONSE_CODES,
    ADVISORY_CODES,
    NOA_CODES,
    _RESP_DEFAULT_CODES,
    _CLAIMS_CODES,
    _OA_CODE_ORDER,
    _MIDDLE_CODE_ORDER,
)
from .client import _get_documents


def _parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.min


def _doc_category(code: str, bundle_type: str) -> str:
    """
    Return the visibility tier for a document code within a given bundle type.

    'default'  — always shown: CTNF, CTFR, NOA, REM, and CLM in initial/granted bundles
    'intclaim' — intermediate claims: CLM in round bundles (hidden by default)
    'extra'    — everything else: OA support, amendments, advisory, RCE (hidden by default)
    """
    if code in OA_TRIGGER_CODES or code in NOA_CODES or code in _RESP_DEFAULT_CODES:
        return "default"
    if code in _CLAIMS_CODES:
        return "default" if bundle_type in ("initial", "granted") else "intclaim"
    return "extra"


def _allowed_categories(show_extra: bool, show_intclaim: bool) -> set:
    cats = {"default"}
    if show_extra:    cats.add("extra")
    if show_intclaim: cats.add("intclaim")
    return cats


def _filter_docs(documents: list, bundle_type: str, show_extra: bool, show_intclaim: bool) -> list:
    """Return only the documents whose category is in the allowed set."""
    allowed = _allowed_categories(show_extra, show_intclaim)
    return [d for d in documents if _doc_category(d["code"], bundle_type) in allowed]


def _find_initial_claims(docs_asc: list) -> dict | None:
    """
    Return the operative initial-claims document (CLM).
    Priority:
      1. CLM within 7 days of a Preliminary Amendment (A.PE)
      2. Earliest INCOMING CLM
      3. Earliest CLM of any direction
    """
    ape_docs = [d for d in docs_asc if d["code"] == "A.PE"]
    if ape_docs:
        ape_date = _parse_date(ape_docs[0]["date"])
        nearby = [
            d for d in docs_asc
            if d["code"] == "CLM"
            and abs((_parse_date(d["date"]) - ape_date).days) <= 7
        ]
        if nearby:
            return min(nearby, key=lambda d: abs((_parse_date(d["date"]) - ape_date).days))

    clms = [d for d in docs_asc if d["code"] == "CLM"]
    if clms:
        incoming = [d for d in clms if d.get("direction", "").upper() == "INCOMING"]
        return (incoming or clms)[0]

    return None


def build_prosecution_bundles(app_no: str) -> list:
    """
    Return a list of bundle dicts (all documents included; filtering happens at display/download time).

    Bundle 0      — Operative initial claims
    Bundle 1..N   — Each OA round (OA trigger + support + applicant response)
    Bundle N+1    — Granted Claims (last CLM after first OA, only when NOA exists)
    """
    raw_docs = _get_documents(app_no)
    if not raw_docs:
        return []

    docs       = sorted(raw_docs, key=lambda d: d["date"])
    oa_anchors = [d for d in docs if d["code"] in OA_TRIGGER_CODES]
    noa_docs   = [d for d in docs if d["code"] in NOA_CODES]

    initial_clm = _find_initial_claims(docs)
    b0_docs = [initial_clm] if initial_clm else []
    if not oa_anchors and noa_docs:
        b0_docs.append(noa_docs[0])

    bundles: list = [{
        "index":     0,
        "label":     "Bundle 0 — Initial Claims",
        "type":      "initial",
        "documents": b0_docs,
    }]

    if not oa_anchors:
        return bundles

    for i, oa in enumerate(oa_anchors):
        is_last   = (i == len(oa_anchors) - 1)
        oa_date   = oa["date"]
        oa_day    = oa_date[:10]
        next_date = oa_anchors[i + 1]["date"] if not is_last else None

        oa_support = sorted(
            [d for d in docs if d["code"] in OA_SUPPORTING_CODES and d["date"][:10] == oa_day],
            key=lambda d: (_OA_CODE_ORDER.get(d["code"], 99), d["date"]),
        )
        window_docs = sorted(
            [
                d for d in docs
                if d["code"] in (RESPONSE_CODES | ADVISORY_CODES)
                and d["date"] > oa_date
                and (next_date is None or d["date"] < next_date)
            ],
            key=lambda d: d["date"],
        )

        round_docs = [oa] + oa_support + window_docs
        if is_last and noa_docs:
            round_docs.append(noa_docs[0])

        oa_type = "Final" if oa["code"] == "CTFR" else "Non-Final"
        label   = f"Bundle {i + 1} — Round {i + 1} ({oa_type})"
        if is_last and noa_docs:
            label += " + NOA"

        bundles.append({
            "index":     i + 1,
            "label":     label,
            "type":      "final_round" if is_last else "round",
            "documents": round_docs,
        })

    first_oa_date = oa_anchors[0]["date"]
    response_clms = sorted(
        [d for d in docs if d["code"] == "CLM" and d["date"] > first_oa_date],
        key=lambda d: d["date"],
    )
    if response_clms and noa_docs:
        granted_idx = len(bundles)
        bundles.append({
            "index":     granted_idx,
            "label":     f"Bundle {granted_idx} — Granted Claims",
            "type":      "granted",
            "documents": [response_clms[-1]],
        })

    return bundles


def _build_three_bundles(bundles: list) -> list:
    """
    Collapse all prosecution rounds into exactly 3 logical groups:

      0 — initial_claims     (Bundle 0 docs)
      1 — {REM-CTNF-...}     (all round-bundle default-tier docs, sorted by date;
                               name built from whichever of REM/CTNF/CTFR/NOA are present)
      2 — granted_claims     (last granted bundle docs)

    Returns a list of dicts: {label, filename, type, documents}
    """
    initial = next((b for b in bundles if b["type"] == "initial"), None)
    granted = next((b for b in bundles if b["type"] == "granted"), None)
    rounds  = [b for b in bundles if b["type"] in ("round", "final_round")]

    middle_docs: list = []
    for b in rounds:
        middle_docs.extend(
            _filter_docs(b["documents"], b["type"], show_extra=False, show_intclaim=False)
        )
    middle_docs.sort(key=lambda d: d["date"])

    present = {d["code"] for d in middle_docs}
    if "ISSUE.NOT" in present:
        present.add("NOA")
    name_parts = [c for c in _MIDDLE_CODE_ORDER if c in present]
    middle_name = "-".join(name_parts) if name_parts else "prosecution"

    return [
        {
            "label":     "Initial Claims",
            "filename":  "Initial_claims",
            "type":      "initial",
            "documents": initial["documents"] if initial else [],
        },
        {
            "label":     middle_name,
            "filename":  middle_name,
            "type":      "round",
            "documents": middle_docs,
        },
        {
            "label":     "Granted Claims",
            "filename":  "Granted_claims",
            "type":      "granted",
            "documents": granted["documents"] if granted else [],
        },
    ]

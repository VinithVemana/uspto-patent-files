"""
ep/bundles.py — Prosecution bundle builder for EP patents
=========================================================

Consumes the flat document list from register_client.list_documents() and
organizes it into logical prosecution bundles — the same shape as the USPTO
bundle builder produces, so downstream (server / CLI / PDF merger) can be
shared.

Bundle shape
------------
Each bundle dict has:
    {
      "index":     int,        # 0 = initial, 1..N = rounds, last = granted
      "label":     str,        # human-readable label
      "type":      str,        # "initial" | "round" | "final_round" | "granted"
      "documents": list[dict]  # docs with code/doc_type/date/doc_id/pages/procedure/category
    }

Each document carries a `code` short-tag (see ep.config.short_code) and a
`category` (default / intclaim / extra) for display filtering.

Bundle rules for EP
-------------------
Bundle 0  — "Initial / International":
    * Filing docs (Claims, Description, Abstract, Drawings, request for grant)
    * European search report + search opinion (direct-EP route)
    * PCT/ISA documents: Written Opinion of ISA, copy of ISR, IPER
      (for PCT-route applications)
    * Pre-examination amendments (Art.19, amendments received before examination)

Bundle 1..N — Each EP examination round:
    * "Communication from the Examining Division" (OA trigger)
    * Applicant response(s): "Reply to communication...", amended claims, etc.
    * Any doc dated between this OA and the next OA

Final Bundle — "Granted" (if applicable):
    * "Intention to grant" + "Decision to grant" + final claims

Refused applications have no granted bundle.
"""

from __future__ import annotations

from . import config

# Fixed order for generating the 3-bundle middle-filename
# Mirrors the USPTO pattern (REM-CTNF-...)
_MIDDLE_CODE_ORDER = ["RESP", "OA", "SUMMON", "GRANT", "REFUSE"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_prosecution_bundles(documents: list[dict]) -> list[dict]:
    """
    Build prosecution bundles from a date-sorted doc list (oldest first).

    Each input doc must have at least: doc_id, date, doc_type, procedure, pages.
    This function mutates docs to add `code` (short tag) and `category` (tier).
    """
    if not documents:
        return []

    # Enrich each doc with code/direction
    docs = sorted(documents, key=lambda d: d["date"])
    for d in docs:
        d["code"]      = config.short_code(d["doc_type"])
        d["direction"] = _infer_direction(d["doc_type"])

    oa_anchors = [d for d in docs if _is_oa_trigger(d)]
    grant_docs = [d for d in docs if _is_grant(d)]
    refuse_docs = [d for d in docs if _is_refusal(d)]

    # --- Bundle 0: Initial / International ---
    initial_docs = _collect_initial_docs(docs, oa_anchors)
    bundles: list[dict] = [{
        "index":     0,
        "label":     "Bundle 0 — Initial / International",
        "type":      "initial",
        "documents": _annotate(initial_docs, "initial"),
    }]

    # --- Bundles 1..N: each EP examination round ---
    for i, oa in enumerate(oa_anchors):
        is_last   = (i == len(oa_anchors) - 1)
        oa_date   = oa["date"]
        next_date = oa_anchors[i + 1]["date"] if not is_last else None

        window_docs = [
            d for d in docs
            if d["date"] > oa_date
            and (next_date is None or d["date"] < next_date)
            and not _is_grant(d)        # grant docs belong in the granted bundle
            and not _is_refusal(d)      # refusal docs handled separately
            and d["date"] != oa_date    # already counted as the anchor
        ]
        # Include any siblings dated on the same day as the OA (annexes, etc.)
        same_day_support = [
            d for d in docs
            if d["date"] == oa_date and d is not oa
            and not _is_oa_trigger(d)
        ]

        round_docs = [oa] + same_day_support + window_docs

        oa_type_tag = _oa_subtype_tag(oa)
        label       = f"Bundle {i + 1} — Round {i + 1}{oa_type_tag}"
        bundle_type = "final_round" if is_last else "round"

        bundles.append({
            "index":     i + 1,
            "label":     label,
            "type":      bundle_type,
            "documents": _annotate(round_docs, bundle_type),
        })

    # --- Granted bundle (if any grant docs exist) ---
    if grant_docs:
        granted_idx = len(bundles)
        bundles.append({
            "index":     granted_idx,
            "label":     f"Bundle {granted_idx} — Granted",
            "type":      "granted",
            "documents": _annotate(sorted(grant_docs, key=lambda d: d["date"]), "granted"),
        })
    elif refuse_docs and not grant_docs:
        # Surface refusal as a terminal bundle so the user can see the decision
        refused_idx = len(bundles)
        bundles.append({
            "index":     refused_idx,
            "label":     f"Bundle {refused_idx} — Refused",
            "type":      "granted",  # reuse type for filtering/naming
            "documents": _annotate(sorted(refuse_docs, key=lambda d: d["date"]), "granted"),
        })

    return bundles


def build_three_bundles(bundles: list[dict]) -> list[dict]:
    """
    Collapse all prosecution rounds into exactly 3 logical bundles:

        0 — initial_claims     (Bundle 0 docs)
        1 — {RESP-OA-...}      (all round-bundle default-tier docs by date;
                                 name built from codes present)
        2 — granted_claims     (last terminal bundle — granted OR refused)

    Same filename/structure contract as the USPTO 3-bundle collapse so
    downstream code can treat both the same.
    """
    initial = next((b for b in bundles if b["type"] == "initial"), None)
    terminal = next((b for b in reversed(bundles) if b["type"] == "granted"), None)
    rounds  = [b for b in bundles if b["type"] in ("round", "final_round")]

    middle_docs: list = []
    for b in rounds:
        middle_docs.extend(_filter_docs(b["documents"], show_extra=False, show_intclaim=False))
    middle_docs.sort(key=lambda d: d["date"])

    present_codes = {d["code"] for d in middle_docs}
    name_parts = [c for c in _MIDDLE_CODE_ORDER if c in present_codes]
    middle_name = "-".join(name_parts) if name_parts else "prosecution"

    terminal_name = "Granted_claims"
    terminal_label = "Granted"
    if terminal and any(_is_refusal(d) for d in terminal["documents"]):
        terminal_name = "refused"
        terminal_label = "Refused"

    return [
        {
            "label":     "Initial / International",
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
            "label":     terminal_label,
            "filename":  terminal_name,
            "type":      "granted",
            "documents": terminal["documents"] if terminal else [],
        },
    ]


def build_four_bundles(documents: list[dict]) -> list[dict]:
    """
    Collapse docs directly into exactly 4 logical bundles for download:

        0 — initial_claims  : bare "Claims" filing doc(s)
        1 — prosecution     : all other docs sorted by date
        2 — granted_claims  : last amended-claims doc(s) before text-for-grant
        3 — patent_document : "Text intended for grant (clean copy)"

    Works on the raw flat document list — no need to call
    build_prosecution_bundles() first. If no text-for-grant exists (still
    pending or refused), bundles 2 and 3 are empty.
    """
    if not documents:
        return [
            {"label": "Initial Claims",  "filename": "Initial_claims",  "type": "initial",         "documents": []},
            {"label": "Prosecution",     "filename": "Prosecution",     "type": "round",            "documents": []},
            {"label": "Granted Claims",  "filename": "Granted_claims",  "type": "granted",          "documents": []},
            {"label": "Patent Document", "filename": "Granted_document", "type": "patent_document", "documents": []},
        ]

    docs = sorted(documents, key=lambda d: d["date"])
    for d in docs:
        d["code"]      = config.short_code(d["doc_type"])
        d["direction"] = _infer_direction(d["doc_type"])

    # Layer 3: text intended for grant
    patent_docs = [d for d in docs if _is_text_for_grant(d)]
    patent_date = patent_docs[0]["date"] if patent_docs else None

    # Layer 0: bare "Claims" filing doc(s) — earliest occurrence only
    filing_docs = [d for d in docs if _is_filing_claims(d)]
    if filing_docs:
        earliest_date = filing_docs[0]["date"]
        initial_docs  = [d for d in filing_docs if d["date"] == earliest_date]
    else:
        initial_docs = []

    # Layer 2: last amended-claims doc(s) strictly before the patent_document date
    if patent_date:
        pre_grant = [
            d for d in docs
            if d["date"] < patent_date
            and not _is_text_for_grant(d)
            and not _is_filing_claims(d)
        ]
        amended = [d for d in pre_grant if _is_amended_claims(d)]
        if amended:
            last_amend_date = amended[-1]["date"]
            granted_docs    = [d for d in amended if d["date"] == last_amend_date]
        else:
            granted_docs = []
    else:
        granted_docs = []

    # Layer 1: everything not in the other three layers
    skip        = {id(d) for d in initial_docs + granted_docs + patent_docs}
    middle_docs = [d for d in docs if id(d) not in skip]

    present_codes = {d["code"] for d in middle_docs}
    name_parts    = [c for c in _MIDDLE_CODE_ORDER if c in present_codes]
    prosecution_name = "-".join(name_parts) if name_parts else "Prosecution"

    return [
        {
            "label":     "Initial Claims",
            "filename":  "Initial_claims",
            "type":      "initial",
            "documents": _annotate(initial_docs, "initial"),
        },
        {
            "label":     prosecution_name,
            "filename":  prosecution_name,
            "type":      "round",
            "documents": _annotate(middle_docs, "round"),
        },
        {
            "label":     "Granted Claims",
            "filename":  "Granted_claims",
            "type":      "granted",
            "documents": _annotate(granted_docs, "granted"),
        },
        {
            "label":     "Patent Document",
            "filename":  "Granted_document",
            "type":      "patent_document",
            "documents": _annotate(patent_docs, "granted"),
        },
    ]


# ---------------------------------------------------------------------------
# Filtering helpers (public)
# ---------------------------------------------------------------------------

def filter_docs(documents: list[dict], bundle_type: str,
                show_extra: bool, show_intclaim: bool) -> list[dict]:
    """Return docs whose (re-classified) tier is in the allowed set."""
    allowed = config.allowed_categories(show_extra, show_intclaim)
    return [d for d in documents if config.classify(d["doc_type"], bundle_type=bundle_type) in allowed]


def _filter_docs(documents: list[dict], show_extra: bool, show_intclaim: bool) -> list[dict]:
    """
    Internal version that uses the docs' pre-annotated `category` field
    (set by _annotate), avoiding a re-classification loop.
    """
    allowed = config.allowed_categories(show_extra, show_intclaim)
    return [d for d in documents if d.get("category", "default") in allowed]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return s.replace("\xa0", " ").strip().lower()


def _is_oa_trigger(doc: dict) -> bool:
    """True if this doc starts a new examination round (only EP Ch.3 exam comms)."""
    t = _norm(doc["doc_type"])
    p = _norm(doc.get("procedure", ""))

    # Only count OA-type docs from the EP examination phase (not ISA/PCT)
    if p not in (config.PROCEDURE_SEARCH_EXAM, config.PROCEDURE_GRANT):
        return False

    # Intention-to-grant is NOT a round trigger (it's the end of examination)
    if "intention to grant" in t:
        return False

    # Summons to oral proceedings opens a new exam phase
    if "summons to attend oral proceedings" in t:
        return True

    # Communications from ED trigger rounds
    if "communication from the examining division" in t:
        return True
    if "communication pursuant to article 94(3)" in t:
        return True

    return False


def _is_grant(doc: dict) -> bool:
    t = _norm(doc["doc_type"])
    return ("decision to grant a european patent" in t
            or "mention of the grant" in t
            or "communication about intention to grant" in t
            or "intention to grant (signatures)" in t
            or "text intended for grant" in t)


def _is_text_for_grant(doc: dict) -> bool:
    return "text intended for grant" in _norm(doc["doc_type"])


def _is_filing_claims(doc: dict) -> bool:
    """True for the bare 'Claims' doc filed at application time (exact type match)."""
    return _norm(doc["doc_type"]) == "claims"


def _is_amended_claims(doc: dict) -> bool:
    return "amended claims" in _norm(doc["doc_type"])


def _is_refusal(doc: dict) -> bool:
    t = _norm(doc["doc_type"])
    return ("decision to refuse the application" in t
            or "application deemed to be withdrawn" in t)


def _infer_direction(doc_type: str) -> str:
    """
    Rough inference of document direction:
      INCOMING  — filed by the applicant
      OUTGOING  — issued by the EPO examining division
      INTERNAL  — annexes, receipts, administrative
    """
    t = _norm(doc_type)
    if any(k in t for k in ("communication from", "decision to",
                            "intention to grant", "summons", "annex to the communication",
                            "european search report", "european search opinion",
                            "invitation", "notification", "notice", "grounds for")):
        return "OUTGOING"
    if any(k in t for k in ("reply to", "amended", "amendments", "claims",
                            "description", "abstract", "drawings", "request for grant",
                            "letter accompanying", "submission", "payment",
                            "written submission", "amendments received")):
        return "INCOMING"
    return "INTERNAL"


def _oa_subtype_tag(oa: dict) -> str:
    t = _norm(oa["doc_type"])
    if "summons" in t:                   return " (Summons)"
    if "article 94(3)" in t:             return " (Art.94(3))"
    return ""


def _annotate(docs: list[dict], bundle_type: str) -> list[dict]:
    """
    Add a `category` field to each doc based on its position in this bundle.
    Returns the same list (docs mutated in-place) for chaining convenience.
    """
    for d in docs:
        d["category"] = config.classify(d["doc_type"], bundle_type=bundle_type)
    return docs


def _collect_initial_docs(docs: list[dict], oa_anchors: list[dict]) -> list[dict]:
    """
    Initial bundle = everything that happened BEFORE the first EP OA,
    plus PCT/ISA docs regardless of date (they're always "pre-examination"
    since PCT Ch.2 precedes EP entry).
    """
    first_oa_date = oa_anchors[0]["date"] if oa_anchors else None

    initial = []
    for d in docs:
        p = _norm(d.get("procedure", ""))

        # PCT/ISA-phase docs always belong in initial
        if p in (config.PROCEDURE_ISA, config.PROCEDURE_RO, config.PROCEDURE_CHAPTER2):
            initial.append(d)
            continue

        # EP examination docs: include if before the first OA
        if first_oa_date is None or d["date"] < first_oa_date:
            # Exclude grant/refusal docs (they belong in the terminal bundle)
            if not _is_grant(d) and not _is_refusal(d):
                initial.append(d)

    return initial

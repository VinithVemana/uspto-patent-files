"""
ep/config.py — EP document-type classifications (USER-EDITABLE)
===============================================================

EPO Register documents have English-language "type" strings rather than the
short codes USPTO uses (CTNF, CTFR, REM, ...). This file maps those strings
to visibility tiers and prosecution-stage buckets.

To change what gets downloaded / displayed:
  - Add or remove substrings in the sets below.
  - Matching is case-insensitive and substring-based — so
      "communication from the examining division"
    will match all of:
      "Communication from the Examining Division"
      "First communication from the Examining Division"
      "Communication from the examining division pursuant to Article 94(3)"

Visibility tiers (mirrors USPTO model):
  default  — always shown (OA triggers, responses, grant/refusal decisions,
             ESR/ISR, filing claims, initial application docs)
  intclaim — intermediate claims filed during prosecution (off by default;
             enable with --show-intclaim)
  extra    — supporting docs: delivery notes, receipts, minutes, oral-proc
             preparation, fee payments, representation (off by default;
             enable with --show-extra)

If you want to classify a specific document type into a different tier,
add the substring to the relevant set below. More specific substrings take
precedence over less-specific ones via the classifier order in `classify()`.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Procedure labels — the "Procedure" column in register.epo.org doclist
# (normalized: non-breaking spaces converted to regular spaces, lowercased)
# ---------------------------------------------------------------------------
PROCEDURE_SEARCH_EXAM = "search / examination"        # EP main prosecution
PROCEDURE_ISA          = "international searching authority"   # PCT: ISA phase
PROCEDURE_RO           = "pct receiving office"       # PCT: receiving office
PROCEDURE_CHAPTER2     = "pct chapter 2 procedure"    # PCT: IPEA examination
PROCEDURE_GRANT        = "grant procedure"            # post-grant / opposition


# ---------------------------------------------------------------------------
# OA TRIGGERS — each match starts a new prosecution round (like CTNF/CTFR)
# ---------------------------------------------------------------------------
OA_TRIGGER_TYPES = {
    "communication from the examining division",
    "communication pursuant to article 94(3)",
    "summons to attend oral proceedings",
    # PCT Chapter 2 written opinions — treated as OA triggers in Ch.2 bundles
    "written opinion - points i-viii",
    "written opinion of the ipea",
}


# ---------------------------------------------------------------------------
# APPLICANT RESPONSE TYPES — close an OA round
# ---------------------------------------------------------------------------
RESPONSE_TYPES = {
    "reply to communication from the examining division",
    "reply to communication from examining division",
    "reply to written opinion prepared by the epo",
    "reply to communication pct/ipea",
    "amended claims filed after receipt of",
    "amended claims with annotations",
    "amended description filed after receipt of",
    "amendments received before examination",
    "written submission in preparation to",
    "letter dealing with oral proceedings",
    "pct(ipea): amended sheet",
    # Art.19 PCT amendments
    "amendments of documents (article 19",
}


# ---------------------------------------------------------------------------
# SEARCH PHASE — European search reports, ISR, Written Opinions of ISA
# Always in the "initial" bundle at default tier.
# ---------------------------------------------------------------------------
SEARCH_TYPES = {
    "european search report",
    "extended european search report",
    "european search opinion",
    "copy of the international search report",
    "international search report",
    "written opinion of the isa",
    "supplementary european search report",
    # Ch.2 IPER (international preliminary examination report) lands here
    "international preliminary examination report",
    "copy of the international preliminary examination report",
}


# ---------------------------------------------------------------------------
# FILING DOCS — the original application-as-filed contents
# Go into the "initial" bundle at default tier.
# ---------------------------------------------------------------------------
FILING_TYPES = {
    "request for grant of a european patent",
    "request for entry into the european phase",
    # The actually-operative claims / description from filing.
    # "Claims", "Description", "Abstract", "Drawings" are matched as
    # exact bare types via FILING_EXACT_TYPES below to avoid capturing
    # "Amended claims", "Claims fee" etc.
}

# Exact-match filing types (whole trimmed-lowercase string must equal).
# Keeps "Claims" separate from "Amended claims with annotations".
FILING_EXACT_TYPES = {
    "claims",
    "description",
    "abstract",
    "drawings",
}


# ---------------------------------------------------------------------------
# GRANT DOCS — intention to grant + decision to grant
# Always shown at default tier; form the "granted" bundle.
# ---------------------------------------------------------------------------
GRANT_TYPES = {
    "communication about intention to grant",
    "decision to grant a european patent",
    "intention to grant (signatures)",
    "mention of the grant",
    "text intended for grant",
}


# ---------------------------------------------------------------------------
# REFUSAL DOCS — final refusal decisions
# ---------------------------------------------------------------------------
REFUSAL_TYPES = {
    "decision to refuse the application",
    "grounds for the decision (annex)",
    "application deemed to be withdrawn",
}


# ---------------------------------------------------------------------------
# INTERMEDIATE CLAIMS — claims filed between OA rounds (not initial/granted).
# Hidden by default; shown with --show-intclaim.
# Matched via FILING_EXACT_TYPES but relocated to intclaim tier when the doc
# is *within* a round bundle — handled in classify().
# ---------------------------------------------------------------------------
INTCLAIM_TYPES = {
    "claims",  # exact match only, within a round
}


# ---------------------------------------------------------------------------
# EXTRA — supporting / administrative docs (off by default; --show-extra)
# Order matters: more-specific substrings first so we don't misclassify
# "Annex to the communication" as a trigger.
# ---------------------------------------------------------------------------
EXTRA_TYPES = {
    "annex to the communication",
    "annex to a communication",
    "annex to international preliminary examination report",
    "minutes of the oral proceedings",
    "preparation for oral proceedings",
    "notification concerning the date of oral proceedings",
    "provision of a copy of the minutes",
    "means of redress",
    "acknowledgement of a document",
    "advice of delivery",
    "(electronic) receipt",
    "letter accompanying subsequently filed items",
    "submission concerning representation",
    "payment of fees and costs",
    "document concerning fees and payments",
    "request for extension of time limit",
    "grant of extension of time limit",
    "enquiry as to when a communication",
    "response to enquiry",
    "notification of forthcoming publication",
    "notification on forthcoming publication",
    "invitation to confirm maintenance",
    "maintenance of the application",
    "communication for party to proceedings",
    "request for correction of errors",
    "pct demand form",
    "cover sheet for fax transmission",
    "notification of receipt of demand",
    "notification of documents sent to wipo",
    "notification of election of epo",
    "transmittal of international preliminary",
    "communication regarding the transmission of",
    "communication regarding comments on",
    "reminder period for payment",
    "cds clean up",
    "published international application",
    "priority document",
    "designation of inventor",
    "communication to designated inventor",
    "examination started",
    "decision to allow further processing",
    "request for further processing",
    "closing of application",
    "withdrawal of a request",
    "withdrawal of request",
    "notice drawing attention",
    "result of consultation",
    "despatch of copy of consultation",
    "separate sheet with ipea428",
    "information on entry into european phase",
    "acknowledgement of receipt of electronic submission",
    "non-unity",
    "notification concerning date of oral proceedings",
    "reply to invitation to file a copy of priority",
}


# ===========================================================================
# Classification logic — do not edit below unless extending tiers
# ===========================================================================

def _norm(s: str) -> str:
    """Normalize a doc-type string: non-breaking space → space, lowercase, trim."""
    return s.replace("\xa0", " ").strip().lower()


def _any_substring(hay: str, needles: set[str]) -> bool:
    return any(n in hay for n in needles)


def classify(doc_type: str, *, bundle_type: str = "round") -> str:
    """
    Return the visibility tier for *doc_type* in the context of *bundle_type*.

    bundle_type ∈ {"initial", "round", "final_round", "granted"}

    Precedence (first match wins — most specific wins):
      1. Explicit EXTRA patterns                           → "extra"
         (checked FIRST so "Enquiry as to when a communication from
          the Examining Division" doesn't falsely match the OA trigger
          substring "communication from the examining division")
      2. Grant / refusal / OA-trigger / response / search   → "default"
      3. Exact filing types within initial/granted bundles  → "default"
      4. Exact filing-type "Claims" within a round          → "intclaim"
      5. Everything else                                    → "extra"
    """
    t = _norm(doc_type)

    # Specific EXTRA patterns must win over broad prosecution substrings
    if _any_substring(t, EXTRA_TYPES):       return "extra"

    # Default-tier prosecution core
    if _any_substring(t, OA_TRIGGER_TYPES):  return "default"
    if _any_substring(t, RESPONSE_TYPES):    return "default"
    if _any_substring(t, GRANT_TYPES):       return "default"
    if _any_substring(t, REFUSAL_TYPES):     return "default"
    if _any_substring(t, SEARCH_TYPES):      return "default"
    if _any_substring(t, FILING_TYPES):      return "default"

    # Exact-match filing docs: depends on which bundle
    if t in FILING_EXACT_TYPES:
        return "default" if bundle_type in ("initial", "granted") else "intclaim"

    # Everything else: extra (supporting admin)
    return "extra"


def category_label(tier: str) -> str:
    """Human-readable suffix used in --text listings."""
    return {"default": "", "intclaim": " [int-claim]", "extra": " [extra]"}.get(tier, "")


def allowed_categories(show_extra: bool, show_intclaim: bool) -> set[str]:
    cats = {"default"}
    if show_extra:    cats.add("extra")
    if show_intclaim: cats.add("intclaim")
    return cats


# ---------------------------------------------------------------------------
# Short-code mapping — for filenames + compact display
# Maps a classified doc to a short label like "OA", "RESP", "ESR", etc.
# ---------------------------------------------------------------------------

def short_code(doc_type: str) -> str:
    """Return a short code (4-6 chars) for filename/display purposes."""
    t = _norm(doc_type)
    # EXTRA specific patterns first — same reason as classify():
    # avoid letting "Enquiry as to when a communication from the
    # Examining Division" match the OA substring.
    if _any_substring(t, EXTRA_TYPES):       return "MISC"
    if _any_substring(t, GRANT_TYPES):       return "GRANT"
    if _any_substring(t, REFUSAL_TYPES):     return "REFUSE"
    # RESPONSE before OA_TRIGGER: "Reply to communication from the ED"
    # contains the OA substring "communication from the examining division".
    # The more-specific "reply to..." phrase must win.
    if _any_substring(t, RESPONSE_TYPES):
        if "amended claims" in t or "amended sheet" in t: return "AMND"
        if "reply" in t: return "RESP"
        return "RESP"
    if _any_substring(t, OA_TRIGGER_TYPES):
        if "summons" in t: return "SUMMON"
        if "article 94(3)" in t: return "OA"
        return "OA"
    if _any_substring(t, SEARCH_TYPES):
        if "extended european" in t: return "EESR"
        if "supplementary european" in t: return "SESR"
        if "european search report" in t: return "ESR"
        if "european search opinion" in t: return "ESO"
        if "international search report" in t: return "ISR"
        if "written opinion of the isa" in t: return "WOISA"
        if "international preliminary examination report" in t: return "IPER"
        return "SRCH"
    if t in FILING_EXACT_TYPES:
        return {"claims": "CLM", "description": "DESC",
                "abstract": "ABS", "drawings": "DRW"}[t]
    if _any_substring(t, FILING_TYPES):      return "FILE"
    return "MISC"

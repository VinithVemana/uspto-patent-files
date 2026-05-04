"""
us/llm_disclaimer.py — GPT agent that classifies USPTO Terminal Disclaimer documents.

Two kinds of TD documents on a file wrapper:

  * Filing (PTO/SB/26 / PTO/AIA/25, doc code DIST) — applicant lists prior US
    patent numbers whose term they're disclaiming.
  * Review decision (PTO-2305 / PTOL-1390, doc code DISQ) — examiner stamps
    APPROVED or DISAPPROVED.

Both are scanned PTOL forms. Layouts vary across decades and the printed forms
are often crooked, so regex/checkbox detection is brittle. We OCR the page
text, then ask GPT to classify the document and extract the structured fields.

API key env var is ``OPENAPI_KEY`` (the user's existing convention) with
``OPENAI_API_KEY`` accepted as a fallback. When neither is set, all calls
return safe defaults so the surrounding code degrades gracefully.
"""

import json
import os
import re
import sys
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]


_MODEL = os.environ.get("OPENAI_TD_MODEL", "gpt-4o-mini")
_TIMEOUT = 60.0

_client: Optional["OpenAI"] = None
_unavailable_reason: Optional[str] = None


def _api_key() -> Optional[str]:
    return os.environ.get("OPENAPI_KEY") or os.environ.get("OPENAI_API_KEY")


def is_available() -> bool:
    """Return True iff an OpenAI client can be built (key present + SDK installed)."""
    return _get_client() is not None


def _get_client() -> Optional["OpenAI"]:
    global _client, _unavailable_reason
    if _client is not None:
        return _client
    if OpenAI is None:
        _unavailable_reason = "openai SDK not installed"
        return None
    key = _api_key()
    if not key:
        _unavailable_reason = "OPENAPI_KEY (or OPENAI_API_KEY) not set"
        return None
    try:
        _client = OpenAI(api_key=key, timeout=_TIMEOUT)
    except Exception as exc:
        _unavailable_reason = f"OpenAI client init failed: {exc}"
        return None
    return _client


_SYSTEM_PROMPT = (
    "You are a precise parser of USPTO Terminal Disclaimer documents. "
    "Reply with a JSON object only. Never invent values that are not in the text."
)

_USER_PROMPT = """Classify the following USPTO document and extract structured fields.

A Terminal Disclaimer FILING (typically form PTO/SB/26 or PTO/AIA/25, doc code DIST) is filed by the applicant and lists one or more prior US patent numbers whose term the applicant disclaims.

A Terminal Disclaimer REVIEW DECISION (typically form PTO-2305 or PTOL-1390, doc code DISQ) is issued by the examiner and indicates APPROVED or DISAPPROVED — sometimes via checkboxes ([X] APPROVED), sometimes via footer text ("THIS TERMINAL DISCLAIMER IS APPROVED" / "TDs approved" / "TDs disapproved"), sometimes via explicit narrative.

Return JSON of exactly this shape:
{
  "doc_type": "filing" | "review" | "other",
  "approved": true | false | null,
  "patents": ["<digits-only US patent number>", ...],
  "notes":   "<one short sentence explaining your classification, optional>"
}

Rules:
- "doc_type": "filing" when the document is a TD filed by applicant (lists patent numbers); "review" when it is the examiner's approval/disapproval decision; "other" when the document is some other thing that mentions terminal disclaimers (e.g. an Office Action page) or is unreadable.
- "approved": only relevant when doc_type == "review". Use true for APPROVED, false for DISAPPROVED, null when undetermined or doc_type != "review".
- "patents": digits only — strip commas, kind codes (B1/B2/A1), country prefixes (US), and whitespace. Only include US patent numbers (typically 7 or 8 digits). Empty list when none are listed. Do not include the instant application's own number.
- A document can be both a filing and a review on the same form in old practice; if you must choose, prefer "review" when an approve/disapprove indicator is present.
- If the OCR text is too garbled to determine anything reliably, return doc_type="other".

Document text (OCR may contain artifacts):
---
{text}
---
"""


_DIGITS_RE = re.compile(r"\d+")


def _normalize_patent_no(s: str) -> str:
    """Strip everything except digits."""
    digits = "".join(_DIGITS_RE.findall(s or ""))
    return digits


def classify_document(text: str, log_label: str = "") -> dict:
    """
    Send OCR text to GPT and return ``{doc_type, approved, patents, notes}``.

    Defaults on error/unavailability: ``{"doc_type": "other", "approved": None,
    "patents": [], "notes": "<reason>"}``.
    """
    default = {"doc_type": "other", "approved": None, "patents": [], "notes": ""}
    if not text or not text.strip():
        default["notes"] = "empty OCR text"
        return default

    client = _get_client()
    if client is None:
        default["notes"] = f"LLM unavailable: {_unavailable_reason}"
        return default

    # Truncate extreme inputs — TD forms are tiny (1-3 pages, < 5K chars OCRed).
    # Cap at 30K chars defensively in case OCR ran on a huge attached batch.
    if len(text) > 30_000:
        text = text[:30_000]

    prompt = _USER_PROMPT.replace("{text}", text)
    label = f" [{log_label}]" if log_label else ""

    try:
        resp = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except Exception as exc:
        print(f"  [LLM TD]{label} request failed: {exc}", file=sys.stderr)
        default["notes"] = f"LLM request error: {exc}"
        return default

    raw = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  [LLM TD]{label} JSON decode failed: {exc}; raw={raw[:200]!r}",
              file=sys.stderr)
        default["notes"] = f"JSON decode error: {exc}"
        return default

    doc_type = data.get("doc_type")
    if doc_type not in ("filing", "review", "other"):
        doc_type = "other"

    approved = data.get("approved")
    if approved not in (True, False, None):
        approved = None
    if doc_type != "review":
        approved = None

    raw_patents = data.get("patents") or []
    patents: list[str] = []
    seen: set[str] = set()
    for p in raw_patents:
        digits = _normalize_patent_no(str(p))
        # Plausible US patent numbers are 6–8 digits. Reject anything outside.
        if not (6 <= len(digits) <= 8):
            continue
        if digits in seen:
            continue
        seen.add(digits)
        patents.append(digits)

    notes = data.get("notes") or ""
    if not isinstance(notes, str):
        notes = str(notes)

    print(
        f"  [LLM TD]{label} doc_type={doc_type} approved={approved} "
        f"patents={patents}",
        file=sys.stderr,
    )

    return {
        "doc_type": doc_type,
        "approved": approved,
        "patents":  patents,
        "notes":    notes,
    }

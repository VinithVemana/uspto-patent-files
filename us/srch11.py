"""
us/srch11.py — Granted-claims source: Dolcera Solr (srch11.alexandria-101123)

Fetches the granted claim set directly from the internal Solr collection
instead of merging USPTO `CLM` documents. Solr returns the claims as
USPTO XML markup (`<claims>/<claim>/<claim-text>`), parsed via lxml and
rendered with reportlab.

Used as the primary source for `Granted_claims*.pdf` (main + TD +
continuation) when reachable, with the existing USPTO bundle merge as
fallback. Solr is preferred because it mirrors the published grant
verbatim, whereas the latest CLM doc on the USPTO file wrapper can
include examiner amendments not present in the issued patent.

PDF layout
----------
- Cover header: patent number title + grant date + source line.
- ``<claim-statement>`` (e.g. "What is claimed is:") rendered as a
  bold preamble.
- Each ``<claim>``:
    * Claim number from the ``num`` attribute, rendered bold with a
      hanging indent so wrapped lines align under the body text.
    * Top-level ``<claim-text>`` content as the lead paragraph.
    * Each nested ``<claim-text>`` becomes its own indented sub-paragraph
      (preserving the a/b/c letter prefixes that already live in the XML).
    * Inline ``<claim-ref>`` flattens to its visible text ("claim 1").
- Page footer: "USxxxxxxx · Page N" right-aligned in grey.
"""

import io
import re
import socket
import sys
from urllib.parse import quote_plus

import requests
from lxml import etree
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer


SOLR_HOST = "srch11.dolcera.net"
SOLR_PORT = 12080
SOLR_BASE = f"http://{SOLR_HOST}:{SOLR_PORT}/solr/alexandria-101123/select"
HTTP_TIMEOUT = 15
TCP_TIMEOUT = 2.0

_reachable_cache: bool | None = None


def is_reachable() -> bool:
    """TCP probe srch11:12080 with 2s timeout. Result cached for the process."""
    global _reachable_cache
    if _reachable_cache is not None:
        return _reachable_cache
    try:
        with socket.create_connection((SOLR_HOST, SOLR_PORT), timeout=TCP_TIMEOUT):
            _reachable_cache = True
            print(f"  [srch11] TCP probe to {SOLR_HOST}:{SOLR_PORT} succeeded — "
                  f"will use Solr for granted claims", file=sys.stderr)
    except (OSError, socket.timeout) as exc:
        _reachable_cache = False
        print(f"  [srch11] TCP probe to {SOLR_HOST}:{SOLR_PORT} failed ({exc}) — "
              f"will fall back to USPTO merge for granted claims", file=sys.stderr)
    return _reachable_cache


def fetch_claims_xml(patent_number: str) -> str | None:
    """
    Query Solr for the granted-publication claim XML.

    Returns the first element of the `clm` list, or None when:
      - Solr returns numFound=0
      - the response is malformed / missing the field
      - any network or HTTP error occurs
    Always takes ``clm[0]`` per the indexing contract — additional list
    entries correspond to other publication variants and are ignored.
    """
    q = f'pn:"{patent_number}" AND publication_type:"Granted" AND pnctry:"US"'
    url = f"{SOLR_BASE}?fl=clm,ucid&q={quote_plus(q)}&rows=1&wt=json"
    print(f"    [srch11] querying Solr for US{patent_number} ...", file=sys.stderr)
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    [srch11] query failed: {exc}", file=sys.stderr)
        return None

    num_found = data.get("response", {}).get("numFound", 0)
    docs = data.get("response", {}).get("docs", [])
    print(f"    [srch11] Solr numFound={num_found}", file=sys.stderr)
    if not docs:
        return None
    clm = docs[0].get("clm")
    if not clm or not isinstance(clm, list):
        print(f"    [srch11] response missing 'clm' list field "
              f"(keys={list(docs[0].keys())})", file=sys.stderr)
        return None
    first = clm[0]
    if not isinstance(first, str) or not first.strip():
        print(f"    [srch11] clm[0] is empty/non-string", file=sys.stderr)
        return None
    print(f"    [srch11] clm[0] xml fetched ({len(first):,} chars, "
          f"clm list has {len(clm)} elements — using first)", file=sys.stderr)
    return first


def _walk_claim_text(elem, depth: int):
    """
    Yield ``(depth, text)`` blocks for a single ``<claim-text>`` subtree.

    Lead text = the element's own text plus the flattened content of any
    inline (non-``claim-text``) children — e.g. ``<claim-ref>claim 1</claim-ref>``
    contributes its visible string. Each nested ``<claim-text>`` becomes a
    deeper-depth block via recursion. Tail text after a nested ``<claim-text>``
    is attached at the parent depth so connecting words ("and", "or") survive.
    """
    lead_parts: list[str] = []
    if elem.text:
        lead_parts.append(elem.text)
    for child in elem:
        if child.tag == "claim-text":
            continue
        for t in child.itertext():
            lead_parts.append(t)
        if child.tail:
            lead_parts.append(child.tail)
    lead = re.sub(r"\s+", " ", "".join(lead_parts)).strip()
    if lead:
        yield (depth, lead)

    for child in elem:
        if child.tag != "claim-text":
            continue
        yield from _walk_claim_text(child, depth + 1)
        if child.tail:
            tail = re.sub(r"\s+", " ", child.tail).strip()
            if tail:
                yield (depth, tail)


def parse_claims(xml_str: str) -> tuple[list[dict], str]:
    """
    Parse the ``<claims>`` root into structured blocks + the claim statement.

    Returns ``(claims, statement)`` where:
      - ``claims`` = ``[{"num", "id", "blocks": [(depth, text), ...]}]``
      - ``statement`` = text of ``<claim-statement>`` (e.g. "What is claimed is:")
        or empty string when missing.

    A claim's ``blocks`` always start with a depth-0 lead block (when
    present); deeper blocks correspond to nested ``<claim-text>`` elements
    in document order.
    """
    root = etree.fromstring(xml_str.encode())

    statement_el = root.find("claim-statement")
    statement = ""
    if statement_el is not None:
        statement = re.sub(
            r"\s+", " ", "".join(statement_el.itertext())
        ).strip()

    out: list[dict] = []
    for c in root.findall("claim"):
        top = c.find("claim-text")
        if top is None:
            continue
        blocks = list(_walk_claim_text(top, 0))
        if not blocks:
            continue
        out.append({
            "num":    c.get("num") or "",
            "id":     c.get("id") or "",
            "blocks": blocks,
        })
    return out, statement


def _escape(s: str) -> str:
    """Minimal escape for reportlab Paragraph (treats `<`, `>`, `&` as markup)."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_leading_number(text: str, num_attr: str) -> str:
    """
    Strip a duplicated leading ``"N. "`` from a claim's lead text — many
    USPTO claim XMLs include the number both in the ``num`` attribute and
    inline at the start of the text, so we drop the inline one and render
    the attribute version bold separately.
    """
    try:
        num_int = int(num_attr.lstrip("0") or "0")
    except ValueError:
        num_int = None
    if num_int is None:
        return re.sub(r"^\s*\d+\s*[.)]\s*", "", text)
    return re.sub(rf"^\s*{num_int}\s*[.)]\s*", "", text)


def render_claims_pdf(
    claims: list[dict],
    statement: str,
    patent_number: str,
    grant_date: str | None = None,
) -> io.BytesIO:
    """
    Render parsed claims to a PDF using reportlab. Returns a BytesIO ready
    to write to disk.

    Uses three depth-keyed paragraph styles (lead / sub1 / sub2) with
    hanging indents so the leading ``"1."`` / ``"a."`` markers align in a
    column and the wrapped body text aligns underneath. The claim number
    is rendered bold from the ``num`` attribute, with any duplicated
    inline ``"N. "`` stripped from the lead text.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        title=f"US{patent_number} — Granted Claims",
        author="",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title", parent=styles["Title"],
        fontName="Helvetica-Bold", fontSize=16, leading=20,
        alignment=1, spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "meta", parent=styles["Normal"],
        fontName="Helvetica", fontSize=9, leading=12,
        textColor=colors.HexColor("#555555"),
        alignment=1, spaceAfter=18,
    )
    statement_style = ParagraphStyle(
        "stmt", parent=styles["Normal"],
        fontName="Helvetica-Bold", fontSize=11, leading=15,
        spaceBefore=2, spaceAfter=10,
    )
    lead_style = ParagraphStyle(
        "claim_lead", parent=styles["BodyText"],
        fontName="Helvetica", fontSize=10.5, leading=14,
        leftIndent=26, firstLineIndent=-26,
        spaceBefore=8, spaceAfter=3, alignment=4,  # justified
    )
    sub1_style = ParagraphStyle(
        "claim_sub1", parent=lead_style,
        fontSize=10, leading=13.5,
        leftIndent=54, firstLineIndent=-22,
        spaceBefore=2, spaceAfter=2,
    )
    sub2_style = ParagraphStyle(
        "claim_sub2", parent=sub1_style,
        leftIndent=84, firstLineIndent=-22,
    )

    def style_for(depth: int) -> ParagraphStyle:
        return [lead_style, sub1_style, sub2_style][min(depth, 2)]

    story: list = [
        Paragraph(f"US {_escape(patent_number)} &mdash; Granted Claims",
                  title_style),
        # Paragraph(
        #     f"Grant date: {_escape(grant_date) if grant_date else 'N/A'} "
        #     f"&nbsp;&middot;&nbsp; Source: Dolcera Solr (srch11)",
        #     meta_style,
        # ),
    ]

    if statement:
        story.append(Paragraph(_escape(statement), statement_style))

    for c in claims:
        num_attr = c["num"]
        try:
            num_label = str(int(num_attr.lstrip("0") or "0"))
        except ValueError:
            num_label = num_attr

        for i, (depth, text) in enumerate(c["blocks"]):
            if i == 0 and depth == 0:
                cleaned = _strip_leading_number(text, num_attr)
                body = (
                    f'<b>{_escape(num_label)}.</b>&nbsp;&nbsp;'
                    f'{_escape(cleaned)}'
                )
                story.append(Paragraph(body, lead_style))
            else:
                story.append(Paragraph(_escape(text), style_for(depth)))

        story.append(Spacer(1, 2))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawRightString(
            LETTER[0] - 0.6 * inch, 0.4 * inch,
            f"US{patent_number}  ·  Page {doc_.page}",
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return buf


def build_granted_claims_pdf(
    patent_number: str, grant_date: str | None = None
) -> tuple[io.BytesIO | None, str]:
    """
    End-to-end: reachability → Solr fetch → parse → render.

    Returns ``(pdf_buf, reason)``. On failure ``pdf_buf`` is None and
    ``reason`` explains why so the caller can log it and fall back to the
    USPTO path.
    """
    if not is_reachable():
        return None, "srch11 unreachable"
    xml = fetch_claims_xml(patent_number)
    if xml is None:
        return None, "no Solr match"
    try:
        claims, statement = parse_claims(xml)
    except etree.XMLSyntaxError as exc:
        return None, f"XML parse error: {exc}"
    if not claims:
        return None, "parsed 0 claims"

    sub_blocks = sum(1 for c in claims for d, _ in c["blocks"] if d > 0)
    first_lead = next(
        (t for d, t in claims[0]["blocks"] if d == 0), claims[0]["blocks"][0][1]
    )
    print(f"    [srch11] parsed {len(claims)} claim(s) "
          f"({sub_blocks} sub-elements, statement={'yes' if statement else 'no'}) "
          f"— claim 1 lead: {first_lead[:80]}...", file=sys.stderr)

    try:
        buf = render_claims_pdf(claims, statement, patent_number, grant_date)
        print(f"    [srch11] rendered PDF ({len(buf.getvalue()):,} bytes)",
              file=sys.stderr)
        return buf, "ok"
    except Exception as exc:
        return None, f"render error: {exc}"

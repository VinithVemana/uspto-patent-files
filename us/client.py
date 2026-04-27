"""
us/client.py — USPTO API HTTP helpers and data parsers
"""

import time

import requests

from .config import BASE_API, HEADERS, CONTINUATION_FOLLOW_CODES


def fetch_json(url: str) -> dict | None:
    """GET with retry/backoff; returns parsed JSON or None."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2)
                continue
            return None
    return None


def _get_metadata(app_no: str) -> dict | None:
    data = fetch_json(f"{BASE_API}/{app_no}/meta-data")
    if not data or "patentFileWrapperDataBag" not in data:
        return None
    bag = data["patentFileWrapperDataBag"][0].get("applicationMetaData", {})

    inventors = []
    for inv in bag.get("inventorBag", []):
        loc = ""
        if "correspondenceAddressBag" in inv:
            a = inv["correspondenceAddressBag"][0]
            loc = f"{a.get('cityName', '')}, {a.get('countryName', '')}".strip(", ")
        inventors.append({"name": inv.get("inventorNameText", ""), "location": loc})

    return {
        "application_number": app_no,
        "title":         bag.get("inventionTitle", "N/A"),
        "status":        bag.get("applicationStatusDescriptionText", "N/A"),
        "filing_date":   bag.get("filingDate", ""),
        "examiner":      bag.get("examinerNameText", "Unassigned"),
        "art_unit":      bag.get("groupArtUnitNumber", "N/A"),
        "docket":        bag.get("docketNumber", "N/A"),
        "entity_status": bag.get("entityStatusData", {}).get("businessEntityStatusCategory", "N/A"),
        "app_type":      bag.get("applicationTypeLabelName", "Utility"),
        "patent_number": bag.get("patentNumber"),
        "grant_date":    bag.get("grantDate"),
        "pub_number":    bag.get("earliestPublicationNumber"),
        "pub_date":      bag.get("earliestPublicationDate"),
        "cpc_codes":     bag.get("cpcClassificationBag", []),
        "inventors":     inventors,
        "applicants":    [a.get("applicantNameText", "") for a in bag.get("applicantBag", [])],
    }


def _get_documents(app_no: str) -> list:
    data = fetch_json(f"{BASE_API}/{app_no}/documents")
    results = []
    if not data:
        return results

    for d in data.get("documentBag", []):
        doc_id = d.get("documentIdentifier", "")
        files, pdf_url = [], ""

        if d.get("downloadOptionBag"):
            for opt in d["downloadOptionBag"]:
                mime = opt.get("mimeTypeIdentifier", "UNK")
                if mime == "MS_WORD":
                    mime = "DOCX"
                url = opt.get("downloadUrl", "")
                files.append({"type": mime, "url": url})
                if mime == "PDF":
                    pdf_url = url

        if not files and doc_id:
            pdf_url = f"https://api.uspto.gov/api/v1/download/applications/{app_no}/{doc_id}.pdf"
            files.append({"type": "PDF", "url": pdf_url})

        pages = d.get("pageCount", 0)
        if not pages and d.get("downloadOptionBag"):
            pages = d["downloadOptionBag"][0].get("pageTotalQuantity", 0)

        results.append({
            "code":      d.get("documentCode", "UNK"),
            "desc":      d.get("documentCodeDescriptionText", "Unknown"),
            "date":      d.get("officialDate", ""),
            "direction": d.get("directionCategory", "INTERNAL"),
            "pages":     pages,
            "pdf_url":   pdf_url,
            "files":     files,
        })

    results.sort(key=lambda x: x["date"], reverse=True)
    return results


def _get_continuity(app_no: str) -> list[dict]:
    """
    Returns the full ancestor chain for app_no, filtered to CONTINUATION_FOLLOW_CODES.

    Each entry: {app_no, patent_no, filing_date, relationship, status, child_app_no}
    The USPTO API returns all ancestors (not just the direct parent), so one call
    gives the entire chain back to the root.
    """
    data = fetch_json(f"{BASE_API}/{app_no}/continuity")
    if not data or not data.get("patentFileWrapperDataBag"):
        return []
    bag = data["patentFileWrapperDataBag"][0]
    results = []
    for entry in bag.get("parentContinuityBag", []):
        if entry.get("claimParentageTypeCode") not in CONTINUATION_FOLLOW_CODES:
            continue
        results.append({
            "app_no":       entry.get("parentApplicationNumberText", ""),
            "patent_no":    entry.get("parentPatentNumber", ""),
            "filing_date":  entry.get("parentApplicationFilingDate", ""),
            "relationship": entry.get("claimParentageTypeCodeDescriptionText", ""),
            "status":       entry.get("parentApplicationStatusDescriptionText", ""),
            "child_app_no": entry.get("childApplicationNumberText", ""),
        })
    return results


def _get_attorney(app_no: str) -> dict | None:
    """
    Fetch power-of-attorney record for an application.

    Returns a dict with:
      - firm:            firm name from customer-number correspondence
                         (falls back to first attorney's firm)
      - firm_address:    multi-line address string of the firm
      - attorneys:       list of {first, last, reg_no, firm, category}
      - raw_text:        lowercased concatenation of every name-bearing
                         field — use for substring searches (e.g.
                         "barta" in raw_text and "jones" in raw_text)
    Returns None if the API has no record.
    """
    data = fetch_json(f"{BASE_API}/{app_no}/attorney")
    if not data or not data.get("patentFileWrapperDataBag"):
        return None

    record = data["patentFileWrapperDataBag"][0].get("recordAttorney") or {}

    attorneys = []
    for bag_name in ("powerOfAttorneyBag", "attorneyBag"):
        for a in record.get(bag_name, []):
            addr = (a.get("attorneyAddressBag") or [{}])[0]
            attorneys.append({
                "first":    a.get("firstName", ""),
                "last":     a.get("lastName", ""),
                "reg_no":   a.get("registrationNumber", ""),
                "firm":     addr.get("nameLineOneText", ""),
                "category": a.get("registeredPractitionerCategory", ""),
                "source":   bag_name,
            })

    firm, firm_address = "", ""
    cnc = record.get("customerNumberCorrespondenceData") or {}
    cnc_addrs = cnc.get("powerOfAttorneyAddressBag") or []
    if cnc_addrs:
        a = cnc_addrs[0]
        firm = a.get("nameLineOneText", "")
        firm_address = ", ".join(filter(None, [
            a.get("addressLineOneText", ""),
            a.get("addressLineTwoText", ""),
            a.get("cityName", ""),
            a.get("geographicRegionCode", ""),
            a.get("postalCode", ""),
            a.get("countryName", ""),
        ]))
    elif attorneys:
        firm = attorneys[0]["firm"]

    raw_parts = [firm, firm_address]
    for at in attorneys:
        raw_parts.extend([at["first"], at["last"], at["firm"]])
    raw_text = " | ".join(p for p in raw_parts if p).lower()

    return {
        "firm":         firm,
        "firm_address": firm_address,
        "attorneys":    attorneys,
        "raw_text":     raw_text,
    }

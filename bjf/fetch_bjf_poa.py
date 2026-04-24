"""
bjf/fetch_bjf_poa.py

Read application numbers from anorig.txt, fetch power-of-attorney + prosecution
history from the USPTO ODP API, flag Barta Jones representation, identify the
last office action, whether a response was filed, and whether an NOA issued.
Dump everything to an Excel file.

Usage:
    /Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python \
        bjf/fetch_bjf_poa.py
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

import pandas as pd
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from us.client import _get_attorney, _get_documents  # noqa: E402
from us.config import (  # noqa: E402
    NOA_CODES,
    OA_TRIGGER_CODES,
    RESPONSE_CODES,
)

INPUT_FILE  = Path(__file__).with_name("anorig.txt")
OUTPUT_FILE = Path(__file__).with_name("bjf_results.xlsx")
SLEEP_BETWEEN_CALLS = 0.1  # polite pause between ANs

AN_RE = re.compile(r"US-?(\d{7,9})", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).with_name("bjf_fetch.log")),
        logging.StreamHandler(),
    ],
)
logging.getLogger().handlers[1].setLevel(logging.WARNING)  # console quieter
log = logging.getLogger(__name__)


def load_application_numbers(path: Path) -> list[str]:
    nums: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = AN_RE.search(raw)
        if not m:
            log.warning("No US-<digits> in line: %r", raw)
            continue
        an = m.group(1)
        if an in seen:
            continue
        seen.add(an)
        nums.append(an)
    return nums


def is_bjf(poa_raw_text: str) -> bool:
    t = poa_raw_text.lower()
    return "barta" in t and "jones" in t


def analyze_prosecution(docs: list[dict]) -> dict:
    """
    docs comes from _get_documents() — already sorted newest-first.
    Returns flags + dates for last OA, response-after-OA, NOA.
    """
    # sort oldest-first so "after" comparisons are chronological
    timeline = sorted(docs, key=lambda d: d.get("date", ""))

    last_oa_code = ""
    last_oa_date = ""
    for d in timeline:
        if d.get("code") in OA_TRIGGER_CODES:
            last_oa_code = d["code"]
            last_oa_date = d.get("date", "")

    response_filed = False
    response_code = ""
    response_date = ""
    if last_oa_date:
        for d in timeline:
            if d.get("date", "") <= last_oa_date:
                continue
            if d.get("code") in RESPONSE_CODES:
                response_filed = True
                response_code = d["code"]
                response_date = d.get("date", "")
                break

    noa_issued = False
    noa_date = ""
    for d in timeline:
        if d.get("code") in NOA_CODES:
            noa_issued = True
            noa_date = d.get("date", "")
            break

    return {
        "last_oa_code":   last_oa_code,
        "last_oa_date":   last_oa_date,
        "response_filed": response_filed,
        "response_code":  response_code,
        "response_date":  response_date,
        "noa_issued":     noa_issued,
        "noa_date":       noa_date,
    }


def process_one(app_no: str) -> dict:
    row: dict = {
        "application_number": app_no,
        "poa_firm":           "",
        "poa_firm_address":   "",
        "poa_attorneys":      "",
        "bjf_match":          False,
        "last_oa_code":       "",
        "last_oa_date":       "",
        "response_filed":     False,
        "response_code":      "",
        "response_date":      "",
        "noa_issued":         False,
        "noa_date":           "",
        "error":              "",
    }

    try:
        poa = _get_attorney(app_no)
    except Exception as e:
        poa = None
        row["error"] = f"attorney: {e}"

    if poa:
        row["poa_firm"]         = poa["firm"]
        row["poa_firm_address"] = poa["firm_address"]
        row["poa_attorneys"]    = "; ".join(
            f"{a['first']} {a['last']} ({a['firm']})".strip()
            for a in poa["attorneys"]
        )
        row["bjf_match"] = is_bjf(poa["raw_text"])
    else:
        if not row["error"]:
            row["error"] = "no attorney record"

    try:
        docs = _get_documents(app_no) or []
    except Exception as e:
        docs = []
        prev = row["error"]
        row["error"] = f"{prev}; docs: {e}" if prev else f"docs: {e}"

    row.update(analyze_prosecution(docs))
    return row


def main() -> int:
    if not INPUT_FILE.exists():
        log.error("Input file not found: %s", INPUT_FILE)
        return 1

    ans = load_application_numbers(INPUT_FILE)
    log.info("Loaded %d unique application numbers", len(ans))

    rows: list[dict] = []
    failed = 0
    start = time.time()

    bar = tqdm(ans, desc="BJF PoA fetch", unit="app")
    for an in bar:
        bar.set_postfix_str(an)
        try:
            row = process_one(an)
        except Exception as e:
            log.exception("Unhandled error for %s", an)
            row = {
                "application_number": an,
                "error": f"unhandled: {e}",
                "bjf_match": False,
                "response_filed": False,
                "noa_issued": False,
            }
        if row.get("error"):
            failed += 1
        rows.append(row)
        time.sleep(SLEEP_BETWEEN_CALLS)

    df = pd.DataFrame(rows, columns=[
        "application_number",
        "poa_firm",
        "poa_firm_address",
        "poa_attorneys",
        "bjf_match",
        "last_oa_code",
        "last_oa_date",
        "response_filed",
        "response_code",
        "response_date",
        "noa_issued",
        "noa_date",
        "error",
    ])
    df.to_excel(OUTPUT_FILE, index=False)

    elapsed = time.time() - start
    bjf_count = int(df["bjf_match"].sum())
    noa_count = int(df["noa_issued"].sum())
    resp_count = int(df["response_filed"].sum())

    log.info("=" * 60)
    log.info("Run summary")
    log.info("  Total processed:       %d", len(rows))
    log.info("  Errors (partial/full): %d", failed)
    log.info("  BJF matches:           %d", bjf_count)
    log.info("  Response-after-OA:     %d", resp_count)
    log.info("  NOA issued:            %d", noa_count)
    log.info("  Elapsed:               %.1fs", elapsed)
    log.info("  Output:                %s", OUTPUT_FILE)
    print(
        f"\nDone. {len(rows)} apps | BJF={bjf_count} | "
        f"resp-after-OA={resp_count} | NOA={noa_count} | "
        f"errors={failed} | {elapsed:.1f}s"
    )
    print(f"Output: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

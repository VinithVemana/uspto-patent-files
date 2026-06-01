"""
check_divisionals.py — quick probe: which of the 30 test patents have divisional parents?

Usage:
    python check_divisionals.py
"""
import sys
import time
sys.path.insert(0, "/Users/vinith_macbook_pro/Desktop/python3/uspto-patent-files")

from ep import ops_client

PATENTS = [
    (2025, "EP3714656B1",  "ZTE CORP"),
    (2025, "EP3713135B1",  "HUAWEI TECH CO LTD"),
    (2025, "EP4038985B1",  "QUALCOMM INC"),
    (2025, "EP4099756B1",  "OPPO"),
    (2025, "EP4258698B1",  "OPPO"),
    (2024, "EP3679760B1",  "INTERDIGITAL"),
    (2024, "EP3651432B1",  "ERICSSON"),
    (2024, "EP3297317B1",  "NTT DOCOMO"),
    (2024, "EP4044485B1",  "HUAWEI TECH CO LTD"),
    (2024, "EP3854143B1",  "APPLE INC"),
    (2023, "EP3672348B1",  "SAMSUNG"),
    (2023, "EP3821676B1",  "SAMSUNG"),
    (2023, "EP3352405B1",  "LG ELECTRONICS"),
    (2023, "EP3297346B1",  "HUAWEI TECH CO LTD"),
    (2023, "EP3923646B1",  "HUAWEI TECH CO LTD"),
    (2022, "EP3456074B1",  "NOKIA TECHNOLOGIES"),
    (2022, "EP3595370B1",  "OPPO"),
    (2022, "EP3609255B1",  "OPPO"),
    (2022, "EP3614700B1",  "HUAWEI TECH CO LTD"),
    (2022, "EP3468282B1",  "OPPO"),
    (2021, "EP3622731B1",  "MOTOROLA"),
    (2021, "EP3533246B1",  "ERICSSON"),
    (2021, "EP3461193B1",  "OPPO"),
    (2021, "EP3352485B1",  "HUAWEI TECH CO LTD"),
    (2021, "EP3582540B1",  "QUALCOMM INC"),
    (2020, "EP3357270B1",  "ERICSSON"),
    (2020, "EP3248411B1",  "ERICSSON"),
    (2020, "EP3567772B1",  "HUAWEI TECH CO LTD"),
    (2020, "EP3300549B1",  "MOTOROLA"),
    (2020, "EP3031262B1",  "INTERDIGITAL"),
]

def check(pub_full: str) -> tuple[bool, str]:
    """Returns (has_parent, parent_app_or_pub_no)."""
    # strip kind code for OPS query — OPS epodoc format is EPxxxxxxx (no kind)
    import re
    m = re.match(r"EP(\d+)", pub_full)
    if not m:
        return False, "bad format"
    ep_num = f"EP{m.group(1)}"
    try:
        biblio = ops_client.get_register_biblio(ep_num)
        if biblio is None:
            return False, "no biblio"
        parent = ops_client.extract_divisional_parent(biblio)
        if parent:
            p = parent.get("app_doc_number") or parent.get("pub_doc_number") or "?"
            return True, f"parent={p}"
        return False, "root"
    except Exception as e:
        return False, f"error: {e}"

if __name__ == "__main__":
    has_div = []
    no_div  = []
    errors  = []

    print(f"{'#':>2}  {'Patent':15s}  {'Year'}  {'Assignee':22s}  {'Divisional?':12s}  Detail")
    print("-" * 90)

    for i, (year, pub, assignee) in enumerate(PATENTS, 1):
        is_div, detail = check(pub)
        tag = "YES" if is_div else ("ERR" if "error" in detail else "no")
        print(f"{i:>2}  {pub:15s}  {year}  {assignee:22s}  {tag:12s}  {detail}")
        sys.stdout.flush()
        if is_div:
            has_div.append(pub)
        elif "error" in detail:
            errors.append(pub)
        else:
            no_div.append(pub)
        time.sleep(0.3)  # gentle on OPS rate limits

    print()
    print(f"Has divisional parent : {len(has_div):2d}  {has_div}")
    print(f"Root (no parent)      : {len(no_div):2d}")
    print(f"Errors                : {len(errors):2d}  {errors}")

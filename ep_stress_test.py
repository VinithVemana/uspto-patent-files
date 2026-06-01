"""
ep_stress_test.py — EP bundle stress test: 3-tier concurrency + evaluation

Tests the EP bundle pipeline against 30 3GPP/H04W wireless patents (ETSI-declared,
EP granted, 2020-2025) across three concurrency tiers:

    Tier 1 (patents  1-16): serial   — 1 at a time
    Tier 2 (patents 17-24): parallel — 2 at a time
    Tier 3 (patents 25-30): parallel — 3 at a time

Divisionals in the test set (--divisionals exercised for all patents):
    EP4099756B1  (OPPO 2025)      → parent 20180933845
    EP3651432B1  (ERICSSON 2024)  → parent 20180723505
    EP3923646B1  (HUAWEI 2023)    → parent 20160802567
    EP3582540B1  (QUALCOMM 2021)  → parent 20150738175

Usage:
    python ep_stress_test.py                        # full run + evaluate
    python ep_stress_test.py --eval-only            # skip downloads, re-evaluate
    python ep_stress_test.py --output-dir ./mydir   # custom output dir
    python ep_stress_test.py --dry-run              # print plan, no downloads
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PYTHON = "/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python"
SCRIPT = os.path.join(os.path.dirname(__file__), "bundles_api_ep.py")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(__file__), "ep_stress_test_out")

# ---------------------------------------------------------------------------
# Patent list — 30 3GPP H04W EP granted patents across 2020-2025
# (year, pub_full, assignee, etsi_project, has_divisional_parent)
# ---------------------------------------------------------------------------
TIERS: list[tuple[int, list[tuple]]] = [
    (1, [  # serial — 1 at a time
        (2025, "EP3714656B1",  "ZTE CORP",            "5G",                    False),
        (2025, "EP3713135B1",  "HUAWEI",               "New Radio(NR)",         False),
        (2025, "EP4038985B1",  "QUALCOMM",             "LTE-16/5G-19",          False),
        (2025, "EP4099756B1",  "OPPO",                 "NR",                    True),   # ← divisional
        (2025, "EP4258698B1",  "OPPO",                 "NR",                    False),
        (2024, "EP3679760B1",  "INTERDIGITAL",         "3GPP 5G NR",            False),
        (2024, "EP3651432B1",  "ERICSSON",             "3GPP 5G",               True),   # ← divisional
        (2024, "EP3297317B1",  "NTT DOCOMO",           "3GPP-R13/R15",          False),
        (2024, "EP4044485B1",  "HUAWEI",               "New Radio(NR)",         False),
        (2024, "EP3854143B1",  "APPLE",                "5G",                    False),
        (2023, "EP3672348B1",  "SAMSUNG",              "3GPP 5G NR",            False),
        (2023, "EP3821676B1",  "SAMSUNG",              "3GPP 5G NR",            False),
        (2023, "EP3352405B1",  "LG ELECTRONICS",       "5G/EUTRAN",             False),
        (2023, "EP3297346B1",  "HUAWEI",               "New Radio(NR)",         False),
        (2023, "EP3923646B1",  "HUAWEI",               "New Radio(NR)",         True),   # ← divisional
        (2022, "EP3456074B1",  "NOKIA",                "3GPP-R14/EUTRAN",       False),
    ]),
    (2, [  # parallel — 2 at a time
        (2022, "EP3595370B1",  "OPPO",                 "NR/3GPP",               False),
        (2022, "EP3609255B1",  "OPPO",                 "3GPP/NR",               False),
        (2022, "EP3614700B1",  "HUAWEI",               "New Radio(NR)",         False),
        (2022, "EP3468282B1",  "OPPO",                 "NR",                    False),
        (2021, "EP3622731B1",  "MOTOROLA",             "5G",                    False),
        (2021, "EP3533246B1",  "ERICSSON",             "SECURITY/LTE",          False),
        (2021, "EP3461193B1",  "OPPO",                 "NR",                    False),
        (2021, "EP3352485B1",  "HUAWEI",               "LTE-V/LTE",             False),
    ]),
    (3, [  # parallel — 3 at a time
        (2021, "EP3582540B1",  "QUALCOMM",             "3GPP-R13/R15",          True),   # ← divisional
        (2020, "EP3357270B1",  "ERICSSON",             "3GPP NR Rel 15",        False),
        (2020, "EP3248411B1",  "ERICSSON",             "3GPP 5G/4G",            False),
        (2020, "EP3567772B1",  "HUAWEI",               "New Radio(NR)",         False),
        (2020, "EP3300549B1",  "MOTOROLA",             "",                      False),
        (2020, "EP3031262B1",  "INTERDIGITAL",         "LTE/3GPP-R15",          False),
    ]),
]

EXPECTED_PDFS = ["Initial_claims.pdf", "REM-CTNF-NOA.pdf", "Granted_claims.pdf"]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _run_patent(pub: str, output_dir: str, log_dir: str) -> dict:
    """Download one patent; return result dict."""
    t0 = time.time()
    log_path = os.path.join(log_dir, f"{pub}.log")
    cmd = [
        PYTHON, SCRIPT,
        pub,
        "--download",
        "--divisionals",
        "--output-dir", output_dir,
    ]
    with open(log_path, "w") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)
    elapsed = time.time() - t0
    return {
        "pub":      pub,
        "rc":       proc.returncode,
        "elapsed":  elapsed,
        "log":      log_path,
    }


def run_tier(tier_no: int, patents: list[tuple], concurrency: int,
             output_dir: str, log_dir: str) -> list[dict]:
    pubs = [p[1] for p in patents]
    results: list[dict] = []
    t0 = time.time()
    print(f"\n{'='*70}")
    print(f"TIER {tier_no}  ({len(pubs)} patents, concurrency={concurrency})")
    print(f"{'='*70}")

    if concurrency == 1:
        for pub in pubs:
            print(f"  → {pub} ...", flush=True)
            r = _run_patent(pub, output_dir, log_dir)
            results.append(r)
            status = "ok" if r["rc"] == 0 else f"rc={r['rc']}"
            print(f"     {status}  {r['elapsed']:.1f}s", flush=True)
    else:
        futures = {}
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for pub in pubs:
                print(f"  → {pub} submitted", flush=True)
                f = pool.submit(_run_patent, pub, output_dir, log_dir)
                futures[f] = pub
            for f in as_completed(futures):
                r = f.result()
                results.append(r)
                status = "ok" if r["rc"] == 0 else f"rc={r['rc']}"
                print(f"  ✓ {r['pub']:15s}  {status}  {r['elapsed']:.1f}s", flush=True)

    tier_time = time.time() - t0
    print(f"  Tier {tier_no} total: {tier_time:.1f}s")
    return results


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

def _find_folder(output_dir: str, pub: str) -> Path | None:
    """
    Find the EP folder for a given publication number.
    related.json (written by --divisionals) has source.pub_no we can match.
    Fallback: pub number embedded in folder name.
    """
    import re
    pub_num = re.search(r"(\d{7,})", pub)
    if not pub_num:
        return None
    pub_digits = pub_num.group(1)

    root = Path(output_dir)
    if not root.exists():
        return None

    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        # Check related.json first
        rel = d / "related.json"
        if rel.exists():
            try:
                data = json.loads(rel.read_text())
                if data.get("source", {}).get("pub_no") == pub_digits:
                    return d
            except Exception:
                pass
        # Fallback: folder name contains pub digits
        if pub_digits in d.name:
            return d
    return None


def evaluate(output_dir: str, all_patents: list[tuple]) -> list[dict]:
    rows = []
    for tier_no, tier_list in TIERS:
        for year, pub, assignee, etsi_proj, known_div in tier_list:
            folder = _find_folder(output_dir, pub)

            if folder is None:
                rows.append({
                    "tier": tier_no, "pub": pub, "assignee": assignee,
                    "year": year, "known_div": known_div,
                    "folder": None, "pdfs": {}, "manifest_failures": [],
                    "divisionals_found": 0, "status": "MISSING",
                })
                continue

            # PDFs
            pdfs = {}
            for fname in EXPECTED_PDFS:
                fpath = folder / fname
                pdfs[fname] = fpath.stat().st_size if fpath.exists() else 0

            # manifest
            manifest_failures = []
            mpath = folder / "manifest.json"
            if mpath.exists():
                try:
                    m = json.loads(mpath.read_text())
                    manifest_failures = m.get("failures", [])
                except Exception:
                    pass

            # divisionals
            divs_found = 0
            rel_path = folder / "related.json"
            if rel_path.exists():
                try:
                    rel = json.loads(rel_path.read_text())
                    divs_found = len(rel.get("divisionals", []))
                except Exception:
                    pass

            # status
            missing_pdfs = [f for f, sz in pdfs.items() if sz == 0]
            if not missing_pdfs and not manifest_failures:
                status = "PASS"
            elif not missing_pdfs and manifest_failures:
                status = "PARTIAL"  # files present but manifest logged failures
            elif len(missing_pdfs) == len(EXPECTED_PDFS):
                status = "FAIL"
            else:
                status = "PARTIAL"

            rows.append({
                "tier": tier_no, "pub": pub, "assignee": assignee,
                "year": year, "known_div": known_div,
                "folder": folder.name, "pdfs": pdfs,
                "manifest_failures": manifest_failures,
                "divisionals_found": divs_found,
                "status": status,
            })
    return rows


def print_report(rows: list[dict], run_results: list[dict]) -> None:
    # Build timing map
    timing = {r["pub"]: r["elapsed"] for r in run_results}

    print("\n" + "=" * 110)
    print("EVALUATION REPORT")
    print("=" * 110)
    hdr = (f"{'T':>1}  {'Patent':15s}  {'Year'}  {'Assignee':16s}  "
           f"{'Init':>6}  {'REM':>6}  {'Grnt':>6}  "
           f"{'Divs':>4}  {'Time':>6}  {'Status'}")
    print(hdr)
    print("-" * 110)

    tier_stats: dict[int, dict] = {}
    for row in rows:
        t = row["tier"]
        if t not in tier_stats:
            tier_stats[t] = {"pass": 0, "partial": 0, "fail": 0, "missing": 0, "total": 0}
        tier_stats[t]["total"] += 1
        tier_stats[t][row["status"].lower()] += 1

        div_marker = "★" if row["known_div"] else " "
        pdfs = row["pdfs"]

        def _sz(f: str) -> str:
            sz = pdfs.get(f, 0)
            if sz == 0:
                return "MISS"
            return f"{sz//1024}K"

        init_s = _sz("Initial_claims.pdf")
        pros_s = _sz("REM-CTNF-NOA.pdf")
        grnt_s = _sz("Granted_claims.pdf")
        elapsed = timing.get(row["pub"], 0)
        elapsed_s = f"{elapsed:.0f}s" if elapsed else "-"
        divs = f"{row['divisionals_found']:>4d}" if row["divisionals_found"] else "   -"

        status_col = row["status"]
        if row["manifest_failures"]:
            status_col += f"  ({len(row['manifest_failures'])} fail)"

        print(f"{t:>1}{div_marker} {row['pub']:15s}  {row['year']}  "
              f"{row['assignee']:16s}  "
              f"{init_s:>6}  {pros_s:>6}  {grnt_s:>6}  "
              f"{divs}  {elapsed_s:>6}  {status_col}")

    print("-" * 110)
    print("★ = known divisional parent\n")

    # Tier summary
    print("TIER SUMMARY")
    print(f"  {'Tier':>4}  {'Total':>5}  {'Pass':>5}  {'Partial':>7}  {'Fail':>5}  {'Missing':>7}")
    all_pass = all_partial = all_fail = all_miss = 0
    for t in sorted(tier_stats):
        s = tier_stats[t]
        print(f"  {t:>4}  {s['total']:>5}  {s['pass']:>5}  {s['partial']:>7}  "
              f"{s['fail']:>5}  {s['missing']:>7}")
        all_pass    += s["pass"]
        all_partial += s["partial"]
        all_fail    += s["fail"]
        all_miss    += s["missing"]
    total = all_pass + all_partial + all_fail + all_miss
    print(f"  {'ALL':>4}  {total:>5}  {all_pass:>5}  {all_partial:>7}  "
          f"{all_fail:>5}  {all_miss:>7}")

    # Manifest failures detail
    failed_rows = [r for r in rows if r["manifest_failures"] or r["status"] in ("FAIL", "MISSING")]
    if failed_rows:
        print("\nFAILURES / MISSING DETAIL")
        for row in failed_rows:
            print(f"  {row['pub']}  status={row['status']}")
            for f in row["manifest_failures"]:
                print(f"    - {f.get('filename', '?')}: {f.get('reason', '?')}")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="EP bundle stress test — 30 3GPP H04W patents")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    ap.add_argument("--eval-only",  action="store_true",
                    help="skip downloads, re-evaluate existing output dir")
    ap.add_argument("--dry-run",    action="store_true",
                    help="print plan only, no downloads")
    args = ap.parse_args()

    output_dir = os.path.abspath(args.output_dir)
    log_dir    = os.path.join(output_dir, "_logs")

    all_patents = [p for _, tier in TIERS for p in tier]
    total = sum(len(t) for _, t in TIERS)

    print(f"EP Stress Test — {total} patents, output → {output_dir}")
    print(f"Tiers: {' | '.join(f'T{n}:{len(t)}@{n}' for n, t in TIERS)}")
    if args.dry_run:
        for tier_no, patents in TIERS:
            print(f"\nTier {tier_no} (concurrency={tier_no}):")
            for year, pub, assignee, etsi, div in patents:
                div_s = " ★DIV" if div else ""
                print(f"  {pub}  {year}  {assignee}{div_s}")
        return

    run_results: list[dict] = []
    if not args.eval_only:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(log_dir,    exist_ok=True)
        t_total_start = time.time()
        for tier_no, patents in TIERS:
            results = run_tier(tier_no, patents, concurrency=tier_no,
                               output_dir=output_dir, log_dir=log_dir)
            run_results.extend(results)
        total_elapsed = time.time() - t_total_start
        print(f"\nAll downloads complete. Total time: {total_elapsed:.1f}s")

    print("\nEvaluating output...")
    rows = evaluate(output_dir, all_patents)
    print_report(rows, run_results)


if __name__ == "__main__":
    main()

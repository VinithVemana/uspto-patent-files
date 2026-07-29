"""
us_stress_test.py — USPTO bundle download stress test with per-patent + per-stage timing.

Runs `bundles_api.py <number> --download` serially for each patent in the list,
timestamps every stderr/stdout line (delta from process start) so slow stages are
visible, then reports files downloaded / bytes / failures per patent.

Usage:
    python us_stress_test.py                                   # full run + report
    python us_stress_test.py --eval-only                       # re-report from existing logs/folders
    python us_stress_test.py --output-dir ./mydir               # custom output dir
    python us_stress_test.py --patents US-9307479-B2            # subset
    python us_stress_test.py --dry-run                          # print plan only
    python us_stress_test.py --extra-flags --continuations      # pass extra flags to bundles_api
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PYTHON = "/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python"
HERE = Path(__file__).parent
SCRIPT = HERE / "bundles_api.py"
DEFAULT_OUTPUT = HERE / "us_stress_test_out"

PATENTS = [
    "US-12652718-B2",
    "US-11122575-B2",
    "US-12335844-B1",
    "US-12232007-B2",
    "US-20240089837-A1",
    "US-20250105992-A1",
    "US-9307479-B2",
    "US-11546842-B2",
    "US-11528595-B2",
]


def cli_number(raw: str) -> str:
    """'US-12652718-B2' -> 'US12652718B2' (resolver strips kind codes itself)."""
    return raw.replace("-", "")


def run_one(raw: str, output_dir: Path, log_dir: Path, extra_flags: list[str]) -> dict:
    number = cli_number(raw)
    log_path = log_dir / f"{number}.log"
    cmd = [PYTHON, str(SCRIPT), number, "--download", "--output-dir", str(output_dir), *extra_flags]

    t0 = time.time()
    lines: list[tuple[float, str]] = []
    with subprocess.Popen(
        cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    ) as proc:
        for line in proc.stdout:
            lines.append((time.time() - t0, line.rstrip("\n")))
        rc = proc.wait()
    elapsed = time.time() - t0

    with log_path.open("w") as fh:
        fh.write(f"# cmd: {' '.join(cmd)}\n")
        for dt, line in lines:
            fh.write(f"[{dt:8.2f}s] {line}\n")
        fh.write(f"# exit={rc} elapsed={elapsed:.2f}s\n")

    # biggest gaps between consecutive log lines => slowest stages
    gaps = []
    prev_t, prev_line = 0.0, "<start>"
    for dt, line in lines:
        gaps.append((dt - prev_t, prev_line, line, prev_t))
        prev_t, prev_line = dt, line
    gaps.sort(key=lambda g: -g[0])

    app_no = None
    for _, line in lines:
        m = re.search(r"Application number:\s*(\d+)", line)
        if m:
            app_no = m.group(1)
            break

    return {
        "input": raw,
        "number": number,
        "app_no": app_no,
        "elapsed_s": round(elapsed, 2),
        "exit_code": rc,
        "log": str(log_path),
        "top_gaps": [
            {"seconds": round(g[0], 2), "at": round(g[3], 2), "after": g[1][:160], "before": g[2][:160]}
            for g in gaps[:5] if g[0] >= 1.0
        ],
    }


def inventory(output_dir: Path) -> dict:
    """Map folder -> {pdfs, bytes, failures} for every patent folder under output_dir."""
    out = {}
    if not output_dir.exists():
        return out
    for folder in sorted(p for p in output_dir.iterdir() if p.is_dir()):
        pdfs = sorted(p for p in folder.glob("*.pdf"))
        failures = []
        mf = folder / "manifest.json"
        if mf.exists():
            try:
                failures = json.loads(mf.read_text()).get("failures", []) or []
            except Exception:
                pass
        out[folder.name] = {
            "pdfs": [p.name for p in pdfs],
            "n_pdfs": len(pdfs),
            "bytes": sum(p.stat().st_size for p in pdfs),
            "failures": failures,
            "subdirs": sorted(d.name for d in folder.iterdir() if d.is_dir()),
        }
    return out


def resolve_folder(result: dict, inv: dict) -> str | None:
    """Match on the patent digits (kind code stripped) or on app_no (app_NNNN folders)."""
    raw = result["input"]
    parts = [p for p in raw.split("-") if p and not re.match(r"(?i)^(US|EP)$", p)]
    cands = []
    if parts:
        cands.append(re.sub(r"[^\d]", "", parts[0]))
    if result.get("app_no"):
        cands.append(result["app_no"])
    for c in cands:
        for name in inv:
            if re.sub(r"[^\d]", "", name) == c:
                return name
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--patents", nargs="*", default=None)
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--extra-flags", nargs=argparse.REMAINDER, default=[])
    args = ap.parse_args()

    output_dir = Path(args.output_dir)
    log_dir = output_dir / "_logs"
    patents = args.patents or PATENTS

    if args.dry_run:
        for p in patents:
            print(f"{PYTHON} {SCRIPT} {cli_number(p)} --download --output-dir {output_dir}")
        return 0

    results_path = output_dir / "_results.json"
    if args.eval_only:
        results = json.loads(results_path.read_text()) if results_path.exists() else []
        for r in results:                      # backfill app_no from older result files
            if not r.get("app_no") and Path(r["log"]).exists():
                m = re.search(r"Application number:\s*(\d+)", Path(r["log"]).read_text())
                r["app_no"] = m.group(1) if m else None
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(exist_ok=True)
        results = []
        for i, raw in enumerate(patents, 1):
            print(f"\n=== [{i}/{len(patents)}] {raw} ===", flush=True)
            r = run_one(raw, output_dir, log_dir, args.extra_flags)
            print(f"    {r['elapsed_s']}s  exit={r['exit_code']}", flush=True)
            results.append(r)
            results_path.write_text(json.dumps(results, indent=2))

    inv = inventory(output_dir)

    print("\n" + "=" * 92)
    print(f"{'patent':22} {'time(s)':>9} {'files':>6} {'MB':>8}  folder / notes")
    print("-" * 92)
    total_t = total_f = 0
    total_b = 0
    for r in results:
        folder = resolve_folder(r, inv)
        info = inv.get(folder, {"n_pdfs": 0, "bytes": 0, "failures": []})
        total_t += r["elapsed_s"]
        total_f += info["n_pdfs"]
        total_b += info["bytes"]
        note = folder or "MISSING"
        if info["failures"]:
            note += f"  FAILURES={len(info['failures'])}"
        if r["exit_code"] != 0:
            note += f"  exit={r['exit_code']}"
        print(f"{r['input']:22} {r['elapsed_s']:9.2f} {info['n_pdfs']:6} {info['bytes']/1e6:8.2f}  {note}")
    print("-" * 92)
    print(f"{'TOTAL':22} {total_t:9.2f} {total_f:6} {total_b/1e6:8.2f}")
    print(f"{'MEAN':22} {total_t/max(len(results),1):9.2f}")

    print("\n--- files per patent ---")
    for r in results:
        folder = resolve_folder(r, inv)
        info = inv.get(folder) or {}
        print(f"\n{r['input']}  ({folder})")
        for name in info.get("pdfs", []):
            size = (Path(output_dir) / folder / name).stat().st_size
            print(f"    {size/1e6:7.2f} MB  {name}")
        for f in info.get("failures", []):
            print(f"    FAIL: {f}")

    print("\n--- slowest stages (gaps >= 1s between log lines) ---")
    for r in results:
        if not r["top_gaps"]:
            continue
        print(f"\n{r['input']}  total {r['elapsed_s']}s")
        for g in r["top_gaps"]:
            print(f"    {g['seconds']:7.2f}s @t={g['at']:.1f}  after: {g['after']}")

    print("\nlogs:", log_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
us/manifest.py — Download manifest: skip unchanged / re-attempt missing artifacts on re-runs
"""

import hashlib
import json
import os
from datetime import datetime

MANIFEST_FILE = "manifest.json"


def _doc_fingerprint(docs: list) -> str:
    """16-char SHA-256 of sorted (code, date, pdf_url) triples — detects document-set changes."""
    key = "|".join(
        sorted(f"{d['code']}_{d['date']}_{d.get('pdf_url', '')}" for d in docs)
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_manifest(output_dir: str) -> dict:
    path = os.path.join(output_dir, MANIFEST_FILE)
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_manifest(
    output_dir: str, app_no: str, artifacts: dict, failures: list | None = None
) -> None:
    """
    Persist artifact fingerprints so the next run can skip unchanged files.

    Only entries that actually landed on disk (i.e. whose download succeeded)
    should be passed in ``artifacts``. Failed downloads go into ``failures``
    so the user can see what's missing and the next run re-attempts them.
    """
    path = os.path.join(output_dir, MANIFEST_FILE)
    payload: dict = {
        "app_no":    app_no,
        "saved_at":  datetime.utcnow().isoformat(),
        "artifacts": {
            k: {"filename": v["filename"], "fingerprint": v["fingerprint"]}
            for k, v in artifacts.items()
            if "filename" in v and "fingerprint" in v
        },
    }
    if failures:
        payload["failures"] = failures
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def _needs_download(
    key: str, filename: str, fingerprint: str, manifest: dict, output_dir: str
) -> tuple[bool, str]:
    """
    Return (should_download, reason).

    Downloads when:
      - file is missing on disk
      - artifact not tracked in manifest (e.g. newly added file type)
      - filename changed (e.g. middle bundle gained a new OA code)
      - fingerprint changed (documents updated since last run)
    """
    filepath = os.path.join(output_dir, filename)
    if not os.path.exists(filepath):
        return True, "missing"
    prev = manifest.get("artifacts", {}).get(key)
    if not prev:
        return True, "not in manifest"
    if prev.get("filename") != filename:
        return True, f"renamed (was {prev['filename']})"
    if prev.get("fingerprint") != fingerprint:
        return True, "documents updated"
    return False, "up-to-date"

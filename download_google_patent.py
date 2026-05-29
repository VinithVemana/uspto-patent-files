"""
Download a US granted patent PDF from Google Patents.

Usage:
    python download_google_patent.py US11516691          # strip US prefix automatically
    python download_google_patent.py 11516691            # bare number also works
    python download_google_patent.py US11516691 -o ./pdfs/  # custom output dir
    python download_google_patent.py US10902286B2        # kind code stripped automatically
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests

# Full Chrome UA — bare "Mozilla/5.0" gets 503 bot-detection from Google
GOOGLE_PATENTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _normalize_patent_number(raw: str) -> str:
    """Strip US prefix and kind code, return bare numeric string."""
    # e.g. "US11516691B2" → "11516691", "US10902286" → "10902286"
    m = re.fullmatch(r"(?:US)?(\d+)(?:[A-Z][A-Z0-9]*)?", raw.strip().upper())
    if not m:
        raise ValueError(f"Cannot parse patent number: {raw!r}")
    return m.group(1)


def get_patent_pdf_url(patent_number: str) -> str | None:
    """
    Scrape Google Patents for the patentimages PDF URL.

    Tries kind codes B2 → B1 → (none).  Each gets up to 3 attempts with
    exponential backoff on 429/5xx and bot-detection pages.
    Returns the URL string, or None if not found.
    """
    pdf_regex = (
        r"patentimages\.storage\.googleapis\.com/"
        r"([a-f0-9/]+/US" + re.escape(patent_number) + r"(?:[A-Z][A-Z0-9]*)?\.pdf)"
    )
    for kind_code in ("B2", "B1", ""):
        gp_url = f"https://patents.google.com/patent/US{patent_number}{kind_code}/en"
        for attempt in range(3):
            try:
                r = requests.get(gp_url, headers=GOOGLE_PATENTS_HEADERS, timeout=15)
                if r.status_code == 404:
                    break  # this kind code doesn't exist, try next
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    print(
                        f"  Google Patents HTTP {r.status_code} for "
                        f"US{patent_number}{kind_code} (attempt {attempt + 1}/3)",
                        file=sys.stderr,
                    )
                    if attempt < 2:
                        time.sleep((attempt + 1) * 2)
                        continue
                    break
                if r.status_code != 200:
                    break
                matches = re.findall(pdf_regex, r.text)
                if matches:
                    return f"https://patentimages.storage.googleapis.com/{matches[0]}"
                # 200 but no PDF link — possible bot-detection soft-block
                if "We're sorry" in r.text or "automated" in r.text.lower():
                    print(
                        f"  Bot-detection page for US{patent_number}{kind_code} "
                        f"(attempt {attempt + 1}/3) — waiting...",
                        file=sys.stderr,
                    )
                    if attempt < 2:
                        time.sleep((attempt + 1) * 5)
                        continue
                break
            except requests.RequestException as exc:
                print(
                    f"  Request error for US{patent_number}{kind_code} "
                    f"(attempt {attempt + 1}/3): {exc}",
                    file=sys.stderr,
                )
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
    return None


def download_pdf(url: str, dest: Path) -> None:
    """Stream-download PDF from url → dest."""
    r = requests.get(url, headers=GOOGLE_PATENTS_HEADERS, stream=True, timeout=60)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a US granted patent PDF from Google Patents."
    )
    parser.add_argument(
        "patent",
        help="Patent number — e.g. US11516691, 11516691, US10902286B2",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=".",
        help="Directory to save the PDF (default: current directory)",
    )
    args = parser.parse_args()

    patent_no = _normalize_patent_number(args.patent)
    output_dir = Path(args.output_dir)

    print(f"Looking up US{patent_no} on Google Patents...")
    pdf_url = get_patent_pdf_url(patent_no)

    if not pdf_url:
        print(f"ERROR: PDF not found for US{patent_no}", file=sys.stderr)
        sys.exit(1)

    print(f"Found: {pdf_url}")

    # Preserve the original filename from the URL (includes kind code when present)
    filename = pdf_url.rsplit("/", 1)[-1]
    dest = output_dir / filename

    print(f"Downloading → {dest}")
    download_pdf(pdf_url, dest)
    size_kb = dest.stat().st_size // 1024
    print(f"Saved {dest}  ({size_kb} KB)")


if __name__ == "__main__":
    main()

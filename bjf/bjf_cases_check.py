import requests
import time
import logging
from typing import Iterable, List, Tuple

# ---------------- CONFIG ---------------- #
SOLR_URL = "http://srch11.dolcera.net:12080/solr/alexandria-101123/select"

BASE_FILTERS: List[Tuple[str, str]] = [
    ("fq", 'adyear:(2023 OR 2024 OR 2025 OR 2026)'),
    ("fq", 'ifi_patstat:"pending"'),
    ("fq", 'ls:"FINAL ACTION"'),
    ("q",  'pa:"nokia"'),
]

FIELDS       = ["anorig"]       # fields to extract
UNIQUE_KEY   = "ucidkey"        # from schema/uniquekey endpoint
ROWS         = 1000
OUTPUT_FILE  = "anorig.txt"

TIMEOUT      = 30
MAX_RETRIES  = 3
RETRY_SLEEP  = 2

# ---------------- LOGGING ---------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ---------------- FETCHER ---------------- #
def fetch_solr_cursor(
    url: str,
    base_params: List[Tuple[str, str]],
    fields: List[str],
    unique_key: str,
    rows: int = 1000,
) -> Iterable[dict]:
    """
    Yields Solr docs using cursorMark deep-paging.
    Sort MUST include the uniqueKey — otherwise Solr returns HTTP 400.
    """
    # fl must include uniqueKey so cursor state is consistent
    fl = ",".join(sorted(set(fields) | {unique_key}))

    static_params = list(base_params) + [
        ("fl",   fl),
        ("rows", str(rows)),
        ("sort", f"{unique_key} asc"),
        ("wt",   "json"),
    ]

    session     = requests.Session()
    cursor_mark = "*"
    total_seen  = 0

    while True:
        params = static_params + [("cursorMark", cursor_mark)]

        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = session.get(url, params=params, timeout=TIMEOUT)
                if r.status_code != 200:
                    # Show what Solr actually complained about
                    logging.error(
                        "Solr %s error body: %s",
                        r.status_code, r.text[:500]
                    )
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = r.json()
                break
            except Exception as e:
                logging.warning("Attempt %d failed: %s", attempt, e)
                if attempt == MAX_RETRIES:
                    logging.error("Max retries reached. Aborting.")
                    return
                time.sleep(RETRY_SLEEP)

        resp = data.get("response", {})
        docs = resp.get("docs", [])
        next_cursor = data.get("nextCursorMark")

        if total_seen == 0:
            logging.info("numFound = %s", resp.get("numFound"))

        for d in docs:
            total_seen += 1
            yield d

        # cursor exhausted
        if not next_cursor or next_cursor == cursor_mark:
            logging.info("Cursor exhausted after %d docs.", total_seen)
            return

        cursor_mark = next_cursor


# ---------------- EXTRACTOR ---------------- #
def iter_field_values(docs: Iterable[dict], field: str) -> Iterable[str]:
    """Yield scalar / multi-valued field values as strings."""
    for doc in docs:
        val = doc.get(field)
        if val is None:
            continue
        if isinstance(val, list):
            for v in val:
                if v is not None:
                    yield str(v)
        else:
            yield str(val)


# ---------------- MAIN ---------------- #
def main():
    logging.info("Starting Solr extraction...")

    docs = fetch_solr_cursor(
        url         = SOLR_URL,
        base_params = BASE_FILTERS,
        fields      = FIELDS,
        unique_key  = UNIQUE_KEY,
        rows        = ROWS,
    )

    count = 0
    seen  = set()   # de-dup (anorig can repeat across docs)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for value in iter_field_values(docs, FIELDS[0]):
            if value in seen:
                continue
            seen.add(value)
            f.write(value + "\n")
            count += 1
            if count % 1000 == 0:
                logging.info("Written %d unique values...", count)

    logging.info("Done. Total unique values written: %d -> %s",
                 count, OUTPUT_FILE)


if __name__ == "__main__":
    main()
"""Scrape complete Swedish dataset records from data.europa.eu to a JSONL file.

Scope: search results matching the portal country facet ``se`` (Sweden) or
the contents of the dataportal.se api catalog ``oppnadata``.
field is requested (no ``includes`` list, which silently dropped data such as
data services, source catalogues and temporal coverage). Before a record is
written, every multilingual object, meaning a dict whose keys are all language
codes, is reduced to its ``sv`` and ``en`` entries; everything else is kept as
returned. The underlying dataset files are not downloaded.

Run ``python scripts/data_eu.py`` from the repository root. Results are streamed
to ``data/data_eu/datasets_sweden.jsonl``; each run overwrites the file, and an
interrupted run leaves a partial export. Load it into SQLite with
``python scripts/data_eu_to_sqlite.py``.
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://data.europa.eu/api/hub/search"
PAGE_SIZE = 200
MAX_RETRIES = 5  # Retry transient request failures with exponential backoff.
_COUNTRIES = ["se"]  # Only results classified under Sweden by the country facet.
_CATALOGS = ["oppnadata"]  # Types of catalogues to include in the search.

_FACETS_JSON = {"catalog": _CATALOGS}
FACETS = json.dumps(_FACETS_JSON)


REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "data" / "data_eu" / "datasets_sweden.jsonl"
KEPT_LANGUAGES = {"sv", "en"}

# Language keys seen in data.europa.eu records: the 24 EU languages plus
# Norwegian (no, nb, nn), Icelandic and Ukrainian. An explicit list avoids
# treating ordinary objects with short keys (e.g. temporal {"gte", "lte"}) as
# multilingual.
LANGUAGE_CODES = {
    "bg",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fi",
    "fr",
    "ga",
    "hr",
    "hu",
    "it",
    "lt",
    "lv",
    "mt",
    "nl",
    "pl",
    "pt",
    "ro",
    "sk",
    "sl",
    "sv",
    "nb",
    "nn",
    "no",
    "is",
    "uk",
}


def get_with_retry(url: str, params: dict) -> requests.Response:
    """Fetch an API response, retrying request failures with exponential backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=120)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as error:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2**attempt
            print(f"  request failed ({error}), retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")  # loop always returns or raises


def is_multilingual(value: dict[str, Any]) -> bool:
    return bool(value) and all(key in LANGUAGE_CODES for key in value)


def keep_sv_en(value: Any) -> Any:
    """Return ``value`` with every multilingual object reduced to sv and en."""
    if isinstance(value, dict):
        if is_multilingual(value):
            return {
                key: keep_sv_en(item)
                for key, item in value.items()
                if key in KEPT_LANGUAGES
            }
        return {key: keep_sv_en(item) for key, item in value.items()}
    if isinstance(value, list):
        return [keep_sv_en(item) for item in value]
    return value


def iter_all_datasets() -> Iterator[dict]:
    """Yield complete Swedish dataset records, paging through a search scroll."""
    params = {
        "filter": "dataset",
        "limit": PAGE_SIZE,
        "scroll": "true",
        "facets": FACETS,
    }
    result = get_with_retry(f"{BASE_URL}/search", params).json().get("result", {})
    print(f"{result.get('count')} Swedish datasets in scope", flush=True)
    datasets, scroll_id, total = result.get("results") or [], result.get("scrollId"), 0

    while datasets:
        yield from datasets
        total += len(datasets)
        print(f"{total} datasets retrieved", flush=True)
        if not scroll_id:
            break
        time.sleep(0.5)  # be gentle with the API
        datasets = (
            get_with_retry(f"{BASE_URL}/scroll", {"scrollId": scroll_id})
            .json()
            .get("result", {})
            .get("results")
            or []
        )


def main() -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as output:
        for dataset in iter_all_datasets():
            output.write(json.dumps(keep_sv_en(dataset), ensure_ascii=False) + "\n")
            total += 1
    print(f"Finished: {total} Swedish datasets written to {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()

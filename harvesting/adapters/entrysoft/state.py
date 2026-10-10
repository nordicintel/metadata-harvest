"""Accepted RowStore table structures, run state and per-provider collections.

``state/tables.jsonl.gz`` holds one record per discovered table: its discovery
record, harvest state (status, markers, row count, cost) and, for harvested
tables, the trimmed structure document the dataset rules read. The published
collections are derived from it on every run, so they are never edited directly.
"""

import gzip
import json
from pathlib import Path

from harvesting.storage import JsonlStore, write_jsonl

TABLES = "state/tables.jsonl.gz"
CURRENT = "state/current.jsonl.gz"
REPORT = "reports/latest.json"
LANGUAGE = "sv"


def table_key(record: dict) -> str:
    return f"{record['provider_code']}/{record['dataset_code']}"


def records(path: Path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def load_tables(root: Path) -> dict:
    return {entry["key"]: entry for entry in records(Path(root) / TABLES)}


def save_tables(root: Path, tables: dict) -> None:
    write_jsonl(Path(root) / TABLES, (tables[k] for k in sorted(tables)))


def load_current(root: Path) -> dict:
    return next(
        records(Path(root) / CURRENT),
        {"mapping_version": None, "providers": {}, "dropped": []},
    )


def save_current(root: Path, current: dict) -> None:
    write_jsonl(Path(root) / CURRENT, [current])


def harvested_documents(tables: dict) -> list[dict]:
    """The structure documents of every harvested table, in key order."""
    return [tables[k]["document"] for k in sorted(tables) if tables[k].get("document")]


def collection_store(root: Path) -> JsonlStore:
    """Published documents grouped per provider; identity-keyed, deterministic."""
    return JsonlStore(Path(root) / "metadata", compressed=True)

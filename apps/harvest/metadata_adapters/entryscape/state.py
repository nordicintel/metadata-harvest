"""Run state and per-table harvest records next to the per-table source files.

``state/tables.jsonl.gz`` holds one record per discovered table: its harvest
state (status, markers, row count, cost). The discovery record and the trimmed
structure document live in the table's source file; datasets are grouped from
those by ``parse``, so nothing published is edited directly.
"""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.storage import write_jsonl

logger = logging.getLogger("entryscape_logger")

TABLES = "state/tables.jsonl.gz"
CURRENT = "state/current.jsonl.gz"
REPORT = "reports/latest.json"
LANGUAGE = "sv"
PROVIDER_CODE = "entryscape"
MOVED_FIELDS = ("record", "document")
"""Fields earlier state rows carried; they now live in the source files."""


def table_key(record: dict) -> str:
    return f"{record['provider_code']}/{record['dataset_code']}"


def records(path: Path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def load_tables(root: Path) -> dict:
    tables = {}
    for entry in records(Path(root) / TABLES):
        for field in MOVED_FIELDS:
            entry.pop(field, None)
        tables[entry["key"]] = entry
    logger.debug("Loaded %d known tables from %s", len(tables), Path(root) / TABLES)
    return tables


def save_tables(root: Path, tables: dict) -> None:
    write_jsonl(Path(root) / TABLES, (tables[k] for k in sorted(tables)))
    logger.debug("Saved %d tables to %s", len(tables), Path(root) / TABLES)


def load_current(root: Path) -> dict:
    current = next(records(Path(root) / CURRENT), None)
    if current is None:
        logger.debug("No run state at %s; starting from an empty state", CURRENT)
        current = {"mapping_version": None, "providers": {}, "dropped": []}
    return current


def save_current(root: Path, current: dict) -> None:
    write_jsonl(Path(root) / CURRENT, [current])

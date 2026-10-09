"""Compact current Kolada state and deterministic collections, without validation scans."""

import gzip
import json
from pathlib import Path

from metadata_harvest.storage import write_jsonl

COLLECTION = "metadata/kolada__sv.jsonl.gz"
STATE = "state/current.jsonl.gz"


def records(path: Path):
    if path.exists():
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def load_documents(root: Path) -> dict:
    return {
        d["extension"]["nordicintel"]["dataset_code"]: d
        for d in records(Path(root) / COLLECTION)
    }


def load_state(root: Path) -> dict:
    return next(
        records(Path(root) / STATE),
        {"kpis": {}, "references": {}, "scopes": {}, "pending": {}},
    )


def save(
    root: Path, state: dict, documents: dict, *, write_metadata: bool = True
) -> None:
    if write_metadata:
        write_jsonl(Path(root) / COLLECTION, (documents[k] for k in sorted(documents)))
    write_jsonl(Path(root) / STATE, [state])

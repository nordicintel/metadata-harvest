"""Published UKÄ collection and compact per-indicator state."""

import gzip
import json
from pathlib import Path

from metadata_harvest.storage import write_jsonl

COLLECTION = "metadata/uka__sv.jsonl.gz"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def records(path: Path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def load_documents(root: Path) -> dict:
    """Published documents keyed by dataset code (the indicator id)."""
    return {
        d["extension"]["nordicintel"]["dataset_code"]: d
        for d in records(Path(root) / COLLECTION)
    }


def load_state(root: Path) -> dict:
    return next(
        records(Path(root) / STATE),
        {"mapping_version": None, "indicators": {}, "withheld": {}},
    )


def save(root: Path, state: dict, documents: dict) -> None:
    write_jsonl(
        Path(root) / COLLECTION,
        (documents[k] for k in sorted(documents, key=int)),
    )
    write_jsonl(Path(root) / STATE, [state])

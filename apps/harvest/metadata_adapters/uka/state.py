"""Published UKÄ collection and compact per-indicator state."""

import gzip
import json
from pathlib import Path

from apps.harvest.accepted import (
    collection_name,
    contribution_documents,
    write_metadata,
)
from apps.harvest.storage import write_jsonl
from apps.model.models import Dataset

COLLECTION = "metadata/" + collection_name("uka")
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def records(path: Path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if value.get("@type") == "dcat:Dataset":
                    yield from contribution_documents(Dataset.from_jsonld(value))
                else:
                    yield value


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
    write_metadata(
        Path(root), documents.values(), adapter="uka", empty_providers=["uka"]
    )
    write_jsonl(Path(root) / STATE, [state])

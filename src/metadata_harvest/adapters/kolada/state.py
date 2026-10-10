"""Compact current Kolada state and deterministic collections, without validation scans."""

import gzip
import json
from pathlib import Path

from metadata_harvest.accepted import collection_name, contribution_documents
from metadata_harvest.accepted import write_metadata as write_accepted_metadata
from metadata_harvest.models import Dataset
from metadata_harvest.storage import write_jsonl

COLLECTION = "metadata/" + collection_name("kolada")
STATE = "state/current.jsonl.gz"


def records(path: Path):
    if path.exists():
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if value.get("@type") == "dcat:Dataset":
                    yield from contribution_documents(Dataset.from_jsonld(value))
                else:
                    yield value


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
        write_accepted_metadata(
            Path(root), documents.values(), adapter="kolada", empty_providers=["kolada"]
        )
    write_jsonl(Path(root) / STATE, [state])

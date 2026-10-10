"""Published SDB collections (one per language) and compact per-subject state."""

import gzip
import json
from pathlib import Path

from metadata_harvest.accepted import (
    collection_name,
    contribution_documents,
    write_metadata,
)
from metadata_harvest.models import Dataset
from metadata_harvest.storage import write_jsonl

PROVIDER_CODE = "socialstyrelsen"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def collection(language: str) -> str:
    return "metadata/" + collection_name(PROVIDER_CODE)


def records(path: Path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if value.get("@type") == "dcat:Dataset":
                    yield from contribution_documents(Dataset.from_jsonld(value))
                else:
                    yield value


def load_documents(root: Path, languages) -> dict:
    """Published documents keyed ``<language>/<subject>``."""
    documents = {}
    for language in languages:
        for document in records(Path(root) / collection(language)):
            shared = document["extension"]["nordicintel"]
            documents[f"{shared['language']}/{shared['dataset_code']}"] = document
    return documents


def load_state(root: Path) -> dict:
    return next(
        records(Path(root) / STATE),
        {"mapping_version": None, "subjects": {}, "withheld": {}},
    )


def save(root: Path, state: dict, documents: dict, languages) -> None:
    """Write every configured language's collection, even when it is empty."""
    write_metadata(
        Path(root), documents.values(), adapter="sdb", empty_providers=[PROVIDER_CODE]
    )
    write_jsonl(Path(root) / STATE, [state])

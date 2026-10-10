"""Published analysverktyg collection (Swedish) and compact per-dataset state."""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.accepted import (
    collection_name,
    contribution_documents,
    write_metadata,
)
from apps.harvest.metadata_adapters.dataanalys.api import PROVIDER_CODE
from apps.harvest.storage import write_jsonl
from apps.model.models import Dataset

logger = logging.getLogger("dataanalys_logger")

STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"

__all__ = [
    "PROVIDER_CODE",
    "REPORT",
    "STATE",
    "collection",
    "load_documents",
    "load_state",
    "save",
]


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
    """Published documents keyed ``<language>/<dataset_code>``."""
    documents = {}
    for language in languages:
        before = len(documents)
        for document in records(Path(root) / collection(language)):
            shared = document["extension"]["nordicintel"]
            documents[f"{shared['language']}/{shared['dataset_code']}"] = document
        logger.debug(
            "Loaded %d published %s documents from %s",
            len(documents) - before,
            language,
            root,
        )
    return documents


def load_state(root: Path) -> dict:
    if not (Path(root) / STATE).exists():
        logger.info(
            "No earlier state under %s: treating this as the first harvest", root
        )
    return next(
        records(Path(root) / STATE),
        {"mapping_version": None, "datasets": {}, "withheld": {}},
    )


def save(root: Path, state: dict, documents: dict, languages) -> None:
    """Write every configured language's collection, even when it is empty."""
    logger.info(
        "Saving %d documents, %d state entries and %d withheld to %s",
        len(documents),
        len(state["datasets"]),
        len(state["withheld"]),
        root,
    )
    write_metadata(
        Path(root),
        documents.values(),
        adapter="dataanalys",
        empty_providers=[PROVIDER_CODE],
    )
    write_jsonl(Path(root) / STATE, [state])

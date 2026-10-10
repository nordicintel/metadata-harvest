"""Published Riksbank collections (one per language) and compact per-dataset state."""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.accepted import (
    collection_name,
    contribution_documents,
    write_metadata,
)
from apps.harvest.storage import write_jsonl
from apps.model.models import Dataset

logger = logging.getLogger("riksbank_logger")

PROVIDER_CODE = "riksbank"
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
    """Published documents keyed ``<language>/<dataset_code>``."""
    documents = {}
    for language in languages:
        path = Path(root) / collection(language)
        before = len(documents)
        for document in records(path):
            shared = document["extension"]["nordicintel"]
            documents[f"{shared['language']}/{shared['dataset_code']}"] = document
        logger.debug(
            "Read %d published %s documents from %s%s",
            len(documents) - before,
            language,
            path,
            "" if path.exists() else " (collection does not exist yet)",
        )
    return documents


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    if not path.exists():
        logger.info("No Riksbank state at %s; starting from an empty state", path)
    return next(
        records(path),
        {"mapping_version": None, "datasets": {}, "withheld": {}},
    )


def save(root: Path, state: dict, documents: dict, languages) -> None:
    """Write every configured language's collection, even when it is empty."""
    logger.info(
        "Saving %d published documents and state for %d datasets to %s",
        len(documents),
        len(state.get("datasets", {})),
        root,
    )
    write_metadata(
        Path(root),
        documents.values(),
        adapter="riksbank",
        empty_providers=[PROVIDER_CODE],
    )
    write_jsonl(Path(root) / STATE, [state])

"""Published SDB collections (one per language) and compact per-subject state."""

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

logger = logging.getLogger("sdb_logger")

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
    logger.info(
        "Loaded %d published SDB documents from %s (%s)",
        len(documents),
        Path(root),
        ", ".join(
            f"{language}={sum(key.startswith(f'{language}/') for key in documents)}"
            for language in languages
        ),
    )
    return documents


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    state = next(
        records(path),
        {"mapping_version": None, "subjects": {}, "withheld": {}},
    )
    if not path.exists():
        logger.info("No SDB state at %s; treating this as a first run", path)
    else:
        logger.info(
            "Loaded SDB state: mapping_version=%s, %d tracked subjects, %d withheld",
            state.get("mapping_version"),
            len(state.get("subjects", {})),
            len(state.get("withheld", {})),
        )
    return state


def save(root: Path, state: dict, documents: dict, languages) -> None:
    """Write every configured language's collection, even when it is empty."""
    logger.info(
        "Saving %d SDB documents, %d tracked subjects and state to %s",
        len(documents),
        len(state.get("subjects", {})),
        Path(root),
    )
    write_metadata(
        Path(root), documents.values(), adapter="sdb", empty_providers=[PROVIDER_CODE]
    )
    write_jsonl(Path(root) / STATE, [state])

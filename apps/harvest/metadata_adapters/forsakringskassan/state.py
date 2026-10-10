"""Published Försäkringskassan collection (Swedish) and compact per-table state."""

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

logger = logging.getLogger("forsakringskassan_logger")

PROVIDER_CODE = "forsakringskassan"
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
    """Published documents keyed ``<language>/<report>/<Table>``."""
    documents = {}
    for language in languages:
        for document in records(Path(root) / collection(language)):
            shared = document["extension"]["nordicintel"]
            documents[f"{shared['language']}/{shared['dataset_code']}"] = document
    logger.debug(
        "Loaded %d published documents from %s for languages %s",
        len(documents),
        root,
        list(languages),
    )
    return documents


def load_state(root: Path) -> dict:
    """``reports`` holds each report's accepted change signals (meta and text
    hashes); ``datasets`` each table's check, verification and publication state;
    ``withheld`` the last good documents that are not published."""
    path = Path(root) / STATE
    state = next(
        records(path),
        {"mapping_version": None, "reports": {}, "datasets": {}, "withheld": {}},
    )
    if path.exists():
        logger.info(
            "Loaded state: %d reports, %d tables, %d withheld documents, "
            "mapping version %s",
            len(state["reports"]),
            len(state["datasets"]),
            len(state["withheld"]),
            state["mapping_version"],
        )
    else:
        logger.info("No previous state at %s: every report will be built", path)
    return state


def save(root: Path, state: dict, documents: dict, languages) -> None:
    """Write every configured language's collection, even when it is empty."""
    logger.info(
        "Writing %d published documents and the state (%d tables) to %s",
        len(documents),
        len(state["datasets"]),
        root,
    )
    write_metadata(
        Path(root),
        documents.values(),
        adapter="forsakringskassan",
        empty_providers=[PROVIDER_CODE],
    )
    write_jsonl(Path(root) / STATE, [state])

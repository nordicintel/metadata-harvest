"""Published Försäkringskassan collection (Swedish) and compact per-table state."""

import gzip
import json
from pathlib import Path

from metadata_harvest.storage import write_jsonl

PROVIDER_CODE = "forsakringskassan"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def collection(language: str) -> str:
    return f"metadata/{PROVIDER_CODE}__{language}.jsonl.gz"


def records(path: Path):
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def load_documents(root: Path, languages) -> dict:
    """Published documents keyed ``<language>/<report>/<Table>``."""
    documents = {}
    for language in languages:
        for document in records(Path(root) / collection(language)):
            shared = document["extension"]["nordicintel"]
            documents[f"{shared['language']}/{shared['dataset_code']}"] = document
    return documents


def load_state(root: Path) -> dict:
    """``reports`` holds each report's accepted change signals (meta and text
    hashes); ``datasets`` each table's check, verification and publication state;
    ``withheld`` the last good documents that are not published."""
    return next(
        records(Path(root) / STATE),
        {"mapping_version": None, "reports": {}, "datasets": {}, "withheld": {}},
    )


def save(root: Path, state: dict, documents: dict, languages) -> None:
    """Write every configured language's collection, even when it is empty."""
    for language in languages:
        keys = sorted(k for k in documents if k.split("/", 1)[0] == language)
        write_jsonl(Path(root) / collection(language), (documents[k] for k in keys))
    write_jsonl(Path(root) / STATE, [state])

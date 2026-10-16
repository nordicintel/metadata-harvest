"""Compact per-dataset Riksbank state next to the per-dataset source files."""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.storage import write_jsonl

logger = logging.getLogger("riksbank_logger")

PROVIDER_CODE = "riksbank"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    if not path.exists():
        return {"mapping_version": None, "datasets": {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.loads(stream.readline())
    # Documents are no longer kept in state; the source files remain.
    state.pop("withheld", None)
    state.setdefault("datasets", {})
    return state


def save(root: Path, state: dict) -> None:
    write_jsonl(Path(root) / STATE, [state])

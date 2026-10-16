"""Compact per-dataset analysverktyg state next to the per-dataset source files."""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.metadata_adapters.dataanalys.api import PROVIDER_CODE
from apps.harvest.storage import write_jsonl

logger = logging.getLogger("dataanalys_logger")

STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    if not path.exists():
        logger.info("No previous state at %s: first run", path)
        return {"mapping_version": None, "datasets": {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.loads(stream.readline())
    # Documents are no longer kept in state; the source files remain.
    state.pop("withheld", None)
    state.setdefault("datasets", {})
    logger.info("Loaded state for %d datasets", len(state["datasets"]))
    return state


def save(root: Path, state: dict) -> None:
    write_jsonl(Path(root) / STATE, [state])

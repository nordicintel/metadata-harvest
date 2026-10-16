"""Compact per-table Försäkringskassan state next to the per-table source files."""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.storage import write_jsonl

logger = logging.getLogger("forsakringskassan_logger")

PROVIDER_CODE = "forsakringskassan"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def load_state(root: Path) -> dict:
    """``reports`` holds each report's accepted change signals (meta and text
    hashes); ``datasets`` each table's check, verification and publication state."""
    path = Path(root) / STATE
    if not path.exists():
        logger.info("No previous state at %s: every report will be built", path)
        return {"mapping_version": None, "reports": {}, "datasets": {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.loads(stream.readline())
    # Documents are no longer kept in state; the source files remain.
    state.pop("withheld", None)
    state.setdefault("reports", {})
    state.setdefault("datasets", {})
    logger.info(
        "Loaded state: %d reports, %d tables, mapping version %s",
        len(state["reports"]),
        len(state["datasets"]),
        state.get("mapping_version"),
    )
    return state


def save(root: Path, state: dict) -> None:
    logger.info("Writing the state (%d tables) to %s", len(state["datasets"]), root)
    write_jsonl(Path(root) / STATE, [state])

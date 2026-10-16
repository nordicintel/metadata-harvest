"""Compact per-subject SDB state next to the per-subject source files."""

import gzip
import json
import logging
from pathlib import Path

from apps.harvest.storage import write_jsonl

logger = logging.getLogger("sdb_logger")

PROVIDER_CODE = "socialstyrelsen"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    if not path.exists():
        logger.info("No SDB state at %s; treating this as a first run", path)
        return {"subjects": {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.loads(stream.readline())
    # Documents are no longer kept in state; the source files remain.
    state.pop("withheld", None)
    state.setdefault("subjects", {})
    logger.info("Loaded SDB state: %d tracked subjects", len(state["subjects"]))
    return state


def save(root: Path, state: dict) -> None:
    logger.info(
        "Saving SDB state with %d tracked subjects to %s",
        len(state.get("subjects", {})),
        Path(root),
    )
    write_jsonl(Path(root) / STATE, [state])

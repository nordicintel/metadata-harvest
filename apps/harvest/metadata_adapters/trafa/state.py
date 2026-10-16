"""Compact per-dataset Trafikanalys state next to the per-dataset source files."""

import gzip
import json
from pathlib import Path

from apps.harvest.storage import write_jsonl

PROVIDER_CODE = "trafa"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                state = json.loads(line)
                # Documents are no longer kept in state; the source files remain.
                state.pop("withheld", None)
                return state
    return {"datasets": {}}


def save(root: Path, state: dict) -> None:
    write_jsonl(Path(root) / STATE, [state])

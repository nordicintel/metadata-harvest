"""Compact per-indicator UKÄ state next to the per-indicator source files."""

import gzip
import json
from pathlib import Path

from apps.harvest.storage import write_jsonl

PROVIDER_CODE = "uka"
STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"


def load_state(root: Path) -> dict:
    path = Path(root) / STATE
    if not path.exists():
        return {"mapping_version": None, "indicators": {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.loads(stream.readline())
    # Documents are no longer kept in state; the source files remain.
    state.pop("withheld", None)
    state.setdefault("indicators", {})
    return state


def save(root: Path, state: dict) -> None:
    write_jsonl(Path(root) / STATE, [state])

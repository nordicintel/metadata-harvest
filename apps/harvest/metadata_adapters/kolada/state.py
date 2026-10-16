"""Compact current Kolada state next to the per-scope source files."""

import gzip
import json
from pathlib import Path

from apps.harvest.storage import write_jsonl

STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"
PROVIDER_CODE = "kolada"
EMPTY_STATE = {"kpis": {}, "references": {}, "scopes": {}, "pending": {}}
TASKS = {"data": "rescan", "metadata": "rebuild"}


def records(path: Path):
    if path.exists():
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def normalize(state: dict) -> dict:
    """Reduce earlier per-scope probe markers to coverage plus a refresh date."""
    state = {**EMPTY_STATE, **state}
    stamps = []
    for code, entry in list(state["scopes"].items()):
        for key in ("checked_at", "data_checked_at", "coverage_at"):
            if entry.get(key):
                stamps.append(entry[key][:10])
        state["scopes"][code] = {
            "coverage": entry.get("coverage") or {},
            "empty": bool(entry.get("empty")),
        }
    if "refreshed" not in state:
        state["refreshed"] = min(stamps) if stamps else None
    state["pending"] = {
        code: TASKS.get(task, task) for code, task in state["pending"].items()
    }
    return state


def load_state(root: Path) -> dict:
    return normalize(next(records(Path(root) / STATE), {}))


def save(root: Path, state: dict) -> None:
    write_jsonl(Path(root) / STATE, [state])

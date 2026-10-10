"""Compact current Kolada state and deterministic collections, without validation scans."""

import gzip
import json
from pathlib import Path

from apps.harvest.accepted import collection_name, contribution_documents
from apps.harvest.accepted import write_metadata as write_accepted_metadata
from apps.harvest.storage import write_jsonl
from apps.model.models import Dataset

COLLECTION = "metadata/" + collection_name("kolada")
STATE = "state/current.jsonl.gz"


def records(path: Path):
    if path.exists():
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if value.get("@type") == "dcat:Dataset":
                    yield from contribution_documents(Dataset.from_jsonld(value))
                else:
                    yield value


def load_documents(root: Path) -> dict:
    return {
        d["extension"]["nordicintel"]["dataset_code"]: d
        for d in records(Path(root) / COLLECTION)
    }


EMPTY_STATE = {"kpis": {}, "references": {}, "scopes": {}, "pending": {}}
TASKS = {"data": "rescan", "metadata": "rebuild"}


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


def save(
    root: Path, state: dict, documents: dict, *, write_metadata: bool = True
) -> None:
    if write_metadata:
        write_accepted_metadata(
            Path(root), documents.values(), adapter="kolada", empty_providers=["kolada"]
        )
    write_jsonl(Path(root) / STATE, [state])

"""Tiny preexisting current-state fixture, unrelated to migration tooling."""

import json

from apps.harvest.metadata_adapters.kolada.metadata import (
    build_dataset,
    load_municipalities,
    reference_rows,
)
from apps.harvest.metadata_adapters.kolada.state import load_documents, records, save


def initial_state(source, target):
    if load_documents(target):
        return {"seeded": 2}
    raw = json.loads((source / "kpi.json").read_text(encoding="utf-8"))[0]
    kpi = {k: raw[k] for k in ("id", "title", "description", "has_ou_data")}
    refs = {
        name: reference_rows(
            json.loads((source / f"{name}.json").read_text(encoding="utf-8"))
        )
        for name in ("ou", "municipality_groups", "kpi_groups")
    }
    refs["municipality"] = load_municipalities()
    state = {
        "kpis": {"N1": kpi},
        "references": refs,
        "scopes": {},
        "pending": {},
        "refreshed": "2026-10-08",
    }
    documents = {}
    for scope in ("municipality", "ou"):
        coverage = next(records(source / f"{scope}_coverage.jsonl"))
        code = f"{scope}/N1"
        state["scopes"][code] = {"coverage": coverage, "empty": False}
        documents[code] = build_dataset(
            kpi, scope, coverage, refs, updated="2026-01-02"
        )
    save(target, state, documents)
    return {"seeded": 2}

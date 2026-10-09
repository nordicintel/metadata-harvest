"""Tiny preexisting current-state fixture, unrelated to migration tooling."""

import json

from metadata_harvest.adapters.kolada.metadata import build_dataset, reference_rows
from metadata_harvest.adapters.kolada.state import load_documents, records, save


def initial_state(source, target):
    if load_documents(target):
        return {"seeded": 2}
    raw = json.loads((source / "kpi.json").read_text(encoding="utf-8"))[0]
    kpi = {k: raw[k] for k in ("id", "title", "description", "has_ou_data")}
    refs = {
        name: reference_rows(
            json.loads((source / f"{name}.json").read_text(encoding="utf-8"))
        )
        for name in ("municipality", "ou", "municipality_groups", "kpi_groups")
    }
    state = {"kpis": {"N1": kpi}, "references": refs, "scopes": {}, "pending": {}}
    documents = {}
    for scope in ("municipality", "ou"):
        coverage = next(records(source / f"{scope}_coverage.jsonl"))
        code = f"{scope}/N1"
        entry = {
            "coverage": coverage,
            "modified": "2026-01-02" if scope == "municipality" else None,
            "checked_at": "2026-10-09T04:00:00Z",
            "coverage_at": "2026-10-08T16:00:00Z",
            "periods": list(range(1970, 2032)),
        }
        state["scopes"][code] = entry
        documents[code] = build_dataset(
            kpi, scope, coverage, refs, modified=entry["modified"]
        )
    save(target, state, documents)
    return {"seeded": 2}

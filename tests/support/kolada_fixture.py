"""Tiny preexisting current-state fixture, unrelated to migration tooling."""

import json

from apps.harvest.metadata_adapters.kolada.metadata import (
    context_digest,
    context_for,
    load_municipalities,
    reference_rows,
)
from apps.harvest.metadata_adapters.kolada.parse import stored_key
from apps.harvest.metadata_adapters.kolada.state import PROVIDER_CODE, records, save
from apps.harvest.source_store import SourceStore

STAMP = "2026-10-08T00:00:00+00:00"


def store_of(target):
    return SourceStore(target / "source", PROVIDER_CODE)


def initial_state(source, target):
    if store_of(target).entries():
        return {"seeded": 2}
    rows = {
        name: json.loads((source / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("kpi", "ou", "municipality_groups", "kpi_groups")
    }
    raw = rows["kpi"][0]
    kpi = {k: raw[k] for k in ("id", "title", "description", "has_ou_data")}
    refs = {
        name: reference_rows(rows[name])
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
    store = store_of(target)
    store.put_shared(PROVIDER_CODE, "kpi.json", [kpi], retrieved_at=STAMP)
    for name in ("ou", "municipality_groups", "kpi_groups"):
        if rows[name]:
            store.put_shared(
                PROVIDER_CODE, f"{name}.json", rows[name], retrieved_at=STAMP
            )
    for scope in ("municipality", "ou"):
        coverage = next(records(source / f"{scope}_coverage.jsonl"))
        code = f"{scope}/N1"
        state["scopes"][code] = {"coverage": coverage, "empty": False}
        store.put(
            stored_key(code),
            responses={"kpi": kpi},
            derived={
                "coverage": coverage,
                "updated": "2026-01-02",
                "references_digest": context_digest(
                    context_for(kpi, scope, coverage, refs)
                ),
            },
            updated=kpi.get("publication_date"),
            retrieved_at=STAMP,
        )
    store.save()
    save(target, state)
    return {"seeded": 2}

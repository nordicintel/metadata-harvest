"""Tiny preexisting current-state fixture, unrelated to migration tooling."""

import asyncio
import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.harvest.metadata_adapters.kolada.client import PROVIDER_CODE, BudgetExpired
from apps.harvest.metadata_adapters.kolada.mapping import (
    context_digest,
    context_for,
    load_municipalities,
    reference_rows,
)
from apps.harvest.metadata_adapters.kolada.parse import stored_key
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write_jsonl
from harvest import lifecycle
from tests.support.binding import binding
from tests.support.parsing import documents

STAMP = "2026-10-08T00:00:00+00:00"
CLOCK = datetime(2026, 10, 9, 8, tzinfo=UTC)
PROVIDER = binding("kolada", "kolada")


def records(path: Path):
    if path.exists():
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def save(root: Path, state: dict) -> None:
    write_jsonl(Path(root) / lifecycle.STATE, [state])


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


@pytest.fixture
def seed_source(tmp_path):
    source = tmp_path / "snapshot"
    source.mkdir()
    files = {
        "kpi.json": [
            {
                "id": "N1",
                "title": "Test",
                "description": "Källa: SCB.",
                "has_ou_data": True,
                "title_en": "Portal title",
            }
        ],
        "ou.json": [{"id": "O1", "title": "Unit", "municipality": "0180"}],
        "kpi_groups.json": [],
        "municipality_groups.json": [],
    }
    for name, value in files.items():
        (source / name).write_text(json.dumps(value), encoding="utf-8")
    coverage = {
        "years": [2025],
        "genders": ["T"],
        "municipality_ids": ["0180"],
        "municipality_group_ids": [],
    }
    (source / "municipality_coverage.jsonl").write_text(
        json.dumps(coverage) + "\n", encoding="utf-8"
    )
    (source / "ou_coverage.jsonl").write_text(
        json.dumps({"years": [2025], "genders": ["M", "K", "T"], "ou_ids": ["O1"]})
        + "\n",
        encoding="utf-8",
    )
    return source


def load_documents(root):
    """Parsed documents keyed ``<scope>/<kpi>``."""
    parsed = documents("kolada", root, [PROVIDER])
    return {key.split("/", 1)[1]: document for key, document in parsed.items()}


def cell(kpi, period, entity, gender="T", deleted=False):
    return {
        "kpi": kpi,
        "period": period,
        "entity": entity,
        "gender": gender,
        "deleted": deleted,
    }


class Source:
    """A Kolada answering like KoladaClient from a state's listings."""

    def __init__(self, state):
        self.state = state
        self.changes = {}  # scope -> flattened cells since from_date
        self.current = {}  # "scope/kpi" -> flattened cells of a full rescan
        self.fail = set()
        self.scans = []
        self.scanned = []
        self.broken_listing = False
        self.broken_changes = set()
        self.budget_out = False

    async def listing(self, name):
        if name == "kpi":
            if self.broken_listing:
                raise ValueError("missing catalogue page")
            return list(self.state["kpis"].values())
        return list(self.state["references"][name].values())

    async def changed_records(self, scope, ids, years, from_date):
        self.scanned.append((scope, len(ids), years[0], years[-1], from_date))
        if scope in self.broken_changes:
            raise ValueError("window failed")
        return list(self.changes.get(scope, []))

    async def scope_records(self, scope, kpi, years):
        code = f"{scope}/{kpi}"
        self.scans.append(code)
        if self.budget_out:
            raise BudgetExpired("budget")
        if code in self.fail:
            raise ValueError("one bad KPI")
        return list(self.current.get(code, []))


def execute(source, data, output, **selection):
    """Run the adapter against ``source`` and collect the output into ``data``."""
    from apps.harvest.metadata_adapters.kolada.harvest import ADAPTER

    result = asyncio.run(
        lifecycle.run(
            ADAPTER,
            PROVIDER,
            data,
            output,
            selection=selection,
            client=source,
            clock=CLOCK,
        )
    )
    lifecycle.collect(ADAPTER, data, output)
    return result

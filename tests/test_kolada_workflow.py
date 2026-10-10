"""Incremental Kolada lifecycle against a deterministic fake source."""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from metadata_harvest.adapters.kolada.state import load_documents, load_state
from tests.kolada_fixture import initial_state as import_seed
from tests.test_kolada import seed_source as source_fixture

seed_source = source_fixture


class Source:
    def __init__(self, state):
        self.state = state
        self.hits = set()
        self.fail = set()
        self.fetched = []
        self.removed = False
        self.broken_listing = False

    async def listing(self, name):
        if name == "kpi":
            if self.broken_listing:
                raise ValueError("missing catalogue page")
            return [] if self.removed else list(self.state["kpis"].values())
        return list(self.state["references"][name].values())

    async def kpi(self, identifier):
        return None if self.removed else self.state["kpis"][identifier]

    async def changed(self, scope, ids, years, cutoff):
        return {i for i in ids if f"{scope}/{i}" in self.hits}

    async def modified(self, scope, identifier, years, **kwargs):
        return "2026-10-09"

    async def coverage(self, scope, identifier, years, references):
        code = f"{scope}/{identifier}"
        self.fetched.append(code)
        if code in self.fail:
            raise ValueError("one bad KPI")
        return self.state["scopes"][code]["coverage"]


def execute(source, data, output, *, resume=False):
    from metadata_harvest.adapters.kolada.workflow import collect, run

    result = asyncio.run(
        run(
            {},
            data,
            output,
            client=source,
            clock=datetime(2026, 10, 9, 8, tzinfo=UTC),
            resume=resume,
        )
    )
    collect(data, output)
    return result


def test_unchanged_reuses_seed_without_validation(seed_source, tmp_path, monkeypatch):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    before = (data / "metadata/rka__multilingual__2e4e3dbc2836.jsonl.gz").read_bytes()
    source = Source(load_state(data))
    monkeypatch.setattr(
        "metadata_harvest.adapters.kolada.workflow.validate_metadata",
        lambda *a, **k: pytest.fail("Unchanged seed validation forbidden"),
    )
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["reused"] == 2
    assert source.fetched == []
    assert (
        before
        == (data / "metadata/rka__multilingual__2e4e3dbc2836.jsonl.gz").read_bytes()
    )
    assert not (data / "work/current.json").exists()


def test_scope_failure_retains_then_retries_only_failed(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.hits = {"municipality/N1", "ou/N1"}
    source.fail = {"ou/N1"}
    before = load_documents(data)["ou/N1"]
    result = execute(source, data, tmp_path / "output")
    assert result["status"] == "completed_with_errors"
    assert load_documents(data)["ou/N1"] == before
    assert set(load_state(data)["pending"]) == {"ou/N1"}
    source.hits = set()
    source.fail = set()
    source.fetched = []
    execute(source, data, tmp_path / "retry")
    assert source.fetched == ["ou/N1"]
    assert load_state(data)["pending"] == {}


def test_incomplete_discovery_retains_and_confirmed_removal_deletes(
    seed_source, tmp_path
):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.broken_listing = True
    result = execute(source, data, tmp_path / "partial")
    assert result["errors"]
    assert len(load_documents(data)) == 2
    source.broken_listing = False
    source.removed = True
    execute(source, data, tmp_path / "removed")
    assert load_documents(data) == {}
    assert load_state(data)["scopes"] == {}


def test_metadata_only_change_does_not_fetch_values(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.state["kpis"]["N1"]["title"] = "Changed title"
    execute(source, data, tmp_path / "output")
    assert source.fetched == []
    assert {d["label"] for d in load_documents(data).values()} == {"Changed title"}


def test_stale_artifact_rejected(seed_source, tmp_path):
    from metadata_harvest.adapters.kolada.workflow import collect, run

    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    asyncio.run(run({}, data, tmp_path / "output", client=source))
    (data / "reports").mkdir(exist_ok=True)
    (data / "reports/extra.json").write_text("{}")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "output")


def test_budget_continuation_preserves_selection_and_pending(seed_source, tmp_path):
    from metadata_harvest.adapters.kolada.workflow import collect, run

    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    result = asyncio.run(
        run(
            {},
            data,
            tmp_path / "budget",
            client=source,
            kpis=["N1"],
            scope="ou",
            budget_seconds=0,
        )
    )
    assert result["continue"]
    collect(data, tmp_path / "budget")
    saved = json.loads((data / "work/current.json").read_text())
    assert saved["selection"] == {"kpis": ["N1"], "scope": "ou"}
    result = execute(source, data, tmp_path / "resume", resume=True)
    assert result["counts"]["checked"] == 1
    assert not (data / "work/current.json").exists()


def test_malformed_group_keeps_previous_references_and_other_work(
    seed_source, tmp_path
):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.state["references"]["kpi_groups"] = {"G1": {"id": "G1", "members": None}}
    source.hits = {"ou/N1"}
    result = execute(source, data, tmp_path / "output")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["updated"] == 1
    assert load_state(data)["references"]["kpi_groups"] == {}


def test_repeated_bounded_waves_advance_change_checks(
    seed_source, tmp_path, monkeypatch
):
    from itertools import count

    from metadata_harvest.adapters.kolada import workflow

    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    checks = []
    original = source.changed

    async def changed(scope, *args):
        checks.append(scope)
        return await original(scope, *args)

    source.changed = changed
    for wave in range(2):
        ticks = count()
        monkeypatch.setattr(workflow, "monotonic", lambda ticks=ticks: next(ticks))
        asyncio.run(
            workflow.run(
                {},
                data,
                tmp_path / str(wave),
                client=source,
                budget_seconds=1.5,
                resume=bool(wave),
            )
        )
        workflow.collect(data, tmp_path / str(wave))
    assert checks == ["municipality", "ou"]
    assert not (data / "work/current.json").exists()


def test_failed_scope_does_not_suppress_new_changes_on_next_normal_run(
    seed_source, tmp_path
):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.hits = {"ou/N1"}
    source.fail = {"ou/N1"}
    execute(source, data, tmp_path / "first")
    source.hits = {"municipality/N1"}
    source.fetched = []
    execute(source, data, tmp_path / "second")
    assert source.fetched == ["municipality/N1", "ou/N1"]


def test_normal_run_does_not_inherit_an_old_sample_restriction(seed_source, tmp_path):
    from metadata_harvest.adapters.kolada.workflow import collect, run

    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    asyncio.run(
        run(
            {},
            data,
            tmp_path / "sample",
            client=source,
            kpis=["N1"],
            scope="ou",
            budget_seconds=0,
        )
    )
    collect(data, tmp_path / "sample")
    result = execute(source, data, tmp_path / "normal")
    assert result["counts"]["checked"] == 2


def test_missing_document_rebuilds_from_current_coverage(seed_source, tmp_path):
    from metadata_harvest.adapters.kolada.state import save

    data = tmp_path / "data"
    import_seed(seed_source, data)
    state, documents = load_state(data), load_documents(data)
    expected = documents.pop("municipality/N1")
    save(data, state, documents)
    source = Source(state)
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["updated"] == 1
    assert load_documents(data)["municipality/N1"] == expected
    assert source.fetched == []

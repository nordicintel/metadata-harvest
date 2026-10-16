"""Incremental Kolada lifecycle against a deterministic fake source."""

import asyncio
from datetime import UTC, datetime

import pytest

from apps.harvest.metadata_adapters.kolada.client import BudgetExpired
from apps.harvest.metadata_adapters.kolada.parse import stored_key
from apps.harvest.metadata_adapters.kolada.state import load_state
from apps.harvest.metadata_adapters.kolada.workflow import collect, run
from tests.harvest.test_kolada import cell, load_documents
from tests.harvest.test_kolada import seed_source as source_fixture
from tests.support.kolada_fixture import initial_state as import_seed
from tests.support.kolada_fixture import store_of

seed_source = source_fixture
CLOCK = datetime(2026, 10, 9, 8, tzinfo=UTC)
MANIFEST = "source/manifest.json"


class Source:
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


def execute(source, data, output, **options):
    result = asyncio.run(run({}, data, output, client=source, clock=CLOCK, **options))
    collect(data, output)
    return result


def test_unchanged_reuses_seed_and_advances_refresh(seed_source, tmp_path, monkeypatch):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    before = (data / MANIFEST).read_bytes()
    source = Source(load_state(data))
    result = execute(source, data, tmp_path / "output")
    assert result["status"] == "success"
    assert result["counts"] == {
        "updated": 0,
        "removed": 0,
        "failed": 0,
        "reused": 2,
        "checked": 2,
    }
    assert source.scans == []
    assert source.scanned == [
        ("municipality", 356, 1970, 2028, "2026-10-07"),
        ("ou", 1, 2003, 2026, "2026-10-07"),
    ]
    assert before == (data / MANIFEST).read_bytes()
    assert load_state(data)["refreshed"] == "2026-10-09"
    assert not (data / "work/current.json").exists()


def test_added_cells_extend_coverage_without_rescanning(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.changes["municipality"] = [
        cell("N1", 2026, "0114", "K"),
        cell("N1", 2026, "G2328"),
        cell("N1", 2026, "G99999"),  # not in the inventory
    ]
    source.state["kpis"]["N1"]["publication_date"] = "2027-03-31"
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["updated"] == 2  # KPI record changed: ou rebuilt too
    assert source.scans == []
    document = load_documents(data)["municipality/N1"]
    assert list(document["dimension"]["municipality"]["category"]["index"]) == [
        "0114",
        "0180",
        "G2328",
    ]
    assert document["dimension"]["year"]["category"]["index"] == {"2025": 0, "2026": 1}
    assert "gender" in document["dimension"]
    assert document["updated"] == "2026-03-31"
    coverage = load_state(data)["scopes"]["municipality/N1"]["coverage"]
    assert coverage["municipality_group_ids"] == ["G2328"]


def test_deleted_cells_force_a_clean_rescan(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.changes["ou"] = [cell("N1", 2025, "O1", "T", deleted=True)]
    source.current["ou/N1"] = [cell("N1", 2024, "O1", "T")]
    result = execute(source, data, tmp_path / "output")
    assert source.scans == ["ou/N1"]
    assert result["counts"]["updated"] == 1
    document = load_documents(data)["ou/N1"]
    assert document["dimension"]["year"]["category"]["index"] == {"2024": 0}
    assert "gender" not in document["dimension"]
    assert document["updated"] == "2025-01-01"


def test_rescan_failure_retains_then_retries_only_failed(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.changes["ou"] = [cell("N1", 2025, "O1", deleted=True)]
    source.fail = {"ou/N1"}
    before = load_documents(data)["ou/N1"]
    result = execute(source, data, tmp_path / "output")
    assert result["status"] == "completed_with_errors"
    assert load_documents(data)["ou/N1"] == before
    assert load_state(data)["pending"] == {"ou/N1": "rescan"}
    assert load_state(data)["refreshed"] == "2026-10-09"
    source.changes, source.fail, source.scans = {}, set(), []
    source.current["ou/N1"] = [cell("N1", 2025, "O1")]
    execute(source, data, tmp_path / "retry")
    assert source.scans == ["ou/N1"]
    assert load_state(data)["pending"] == {}


def test_broken_catalogue_or_scan_retains_everything(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.broken_listing = True
    result = execute(source, data, tmp_path / "partial")
    assert [e["resource"] for e in result["errors"]] == ["kpi"]
    assert len(load_documents(data)) == 2
    assert source.scanned == []
    assert load_state(data)["refreshed"] == "2026-10-08"
    source.broken_listing = False
    source.broken_changes = {"ou"}
    result = execute(source, data, tmp_path / "scan")
    assert [e["stage"] for e in result["errors"]] == ["changes"]
    assert load_state(data)["refreshed"] == "2026-10-08"
    assert len(load_documents(data)) == 2


def test_catalogue_removal_deletes_and_new_kpi_is_rescanned(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.state["kpis"] = {"N2": {"id": "N2", "title": "New", "has_ou_data": False}}
    source.current["municipality/N2"] = [cell("N2", 2024, "0180")]
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["removed"] == 2
    assert result["counts"]["updated"] == 1
    assert source.scans == ["municipality/N2"]
    assert set(load_documents(data)) == {"municipality/N2"}
    assert set(store_of(data).entries()) == {stored_key("municipality/N2")}
    assert set(load_state(data)["scopes"]) == {"municipality/N2"}
    assert "N1" not in load_state(data)["kpis"]


def test_truncated_catalogue_is_a_discovery_failure(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.state["kpis"] = {}
    result = execute(source, data, tmp_path / "output")
    assert result["errors"][0]["resource"] == "kpi"
    assert len(load_documents(data)) == 2


def test_metadata_only_change_does_not_fetch_values(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.state["kpis"]["N1"]["title"] = "Changed title"
    execute(source, data, tmp_path / "output")
    assert source.scans == []
    assert {d["label"] for d in load_documents(data).values()} == {"Changed title"}


def test_empty_rescan_removes_document_and_marks_scope_empty(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.changes["municipality"] = [cell("N1", 2025, "0180", deleted=True)]
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["removed"] == 1
    assert set(load_documents(data)) == {"ou/N1"}
    assert load_state(data)["scopes"]["municipality/N1"]["empty"] is True
    source.changes, source.scans = {}, []
    execute(source, data, tmp_path / "again")
    assert source.scans == []


def test_restricted_run_bounds_work_and_keeps_refresh_marker(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.changes["ou"] = [cell("N1", 2025, "O1", deleted=True)]
    result = execute(source, data, tmp_path / "output", kpis=["N1"], scope="ou")
    assert [s[0] for s in source.scanned] == ["ou"]
    assert source.scans == ["ou/N1"]
    assert result["selection"] == {"kpis": ["N1"], "scope": "ou"}
    assert load_state(data)["refreshed"] == "2026-10-08"


def test_budget_expiry_keeps_pending_for_next_run(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    source.changes["ou"] = [cell("N1", 2025, "O1", deleted=True)]
    source.budget_out = True
    result = execute(source, data, tmp_path / "output")
    assert result["status"] == "running"
    assert load_state(data)["pending"] == {"ou/N1": "rescan"}
    assert len(load_documents(data)) == 2


def test_stale_artifact_rejected(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    source = Source(load_state(data))
    asyncio.run(run({}, data, tmp_path / "output", client=source, clock=CLOCK))
    (data / "reports").mkdir(exist_ok=True)
    (data / "reports/extra.json").write_text("{}")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "output")


def test_missing_manifest_entry_is_rebuilt_from_saved_coverage(seed_source, tmp_path):
    data = tmp_path / "data"
    import_seed(seed_source, data)
    state = load_state(data)
    expected = load_documents(data)["municipality/N1"]
    store = store_of(data)
    store.remove(stored_key("municipality/N1"))
    store.save()
    source = Source(state)
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["updated"] == 1
    rebuilt = load_documents(data)["municipality/N1"]
    assert {k: v for k, v in rebuilt.items() if k != "updated"} == {
        k: v for k, v in expected.items() if k != "updated"
    }
    assert rebuilt["updated"] == "2026-01-01"
    assert source.scans == []

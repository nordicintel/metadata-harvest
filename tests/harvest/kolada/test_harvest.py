"""Kolada: the incremental loop against a deterministic fake source."""

from apps.harvest.metadata_adapters.kolada.harvest import load_state
from apps.harvest.metadata_adapters.kolada.parse import stored_key
from tests.support.kolada import (
    Source,
    cell,
    execute,
    initial_state,
    load_documents,
    seed_source,  # noqa: F401  (fixture)
    store_of,
)

MANIFEST = "source/manifest.json"


def test_unchanged_run_reuses_the_seed_and_advances_the_refresh_marker(
    seed_source,  # noqa: F811
    tmp_path,
):
    data = tmp_path / "data"
    initial_state(seed_source, data)
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
    assert result["pending"] == 0 and result["refreshed"] == "2026-10-09"


def test_added_cells_extend_coverage_and_deletions_force_a_rescan(
    seed_source,  # noqa: F811
    tmp_path,
):
    data = tmp_path / "data"
    initial_state(seed_source, data)
    source = Source(load_state(data))
    source.changes["municipality"] = [
        cell("N1", 2026, "0114", "K"),
        cell("N1", 2026, "G2328"),
        cell("N1", 2026, "G99999"),  # not in the inventory
    ]
    source.changes["ou"] = [cell("N1", 2025, "O1", deleted=True)]
    source.current["ou/N1"] = [cell("N1", 2026, "O1")]
    result = execute(source, data, tmp_path / "output")
    assert result["counts"]["updated"] == 2 and source.scans == ["ou/N1"]
    document = load_documents(data)["municipality/N1"]
    assert list(document["dimension"]["municipality"]["category"]["index"]) == [
        "0114",
        "0180",
        "G2328",
    ]
    assert document["dimension"]["year"]["category"]["index"] == {"2025": 0, "2026": 1}
    assert load_state(data)["scopes"]["ou/N1"]["coverage"]["years"] == [2026]


def test_budget_expiry_keeps_pending_work_and_an_empty_state_starts_a_rescan(
    seed_source,  # noqa: F811
    tmp_path,
):
    data = tmp_path / "data"
    initial_state(seed_source, data)
    source = Source(load_state(data))
    source.changes["ou"] = [cell("N1", 2025, "O1", deleted=True)]
    source.budget_out = True
    result = execute(source, data, tmp_path / "output")
    assert result["status"] == "running"
    assert load_state(data)["pending"] == {"ou/N1": "rescan"}
    assert len(load_documents(data)) == 2
    assert stored_key("ou/N1") in store_of(data).entries()

    fresh = tmp_path / "fresh"
    source = Source(load_state(data))
    source.current["municipality/N1"] = [cell("N1", 2026, "0180")]
    result = execute(source, fresh, tmp_path / "fresh-output")
    assert result["status"] == "success"
    assert sorted(source.scans) == ["municipality/N1", "ou/N1"]
    assert result["counts"]["updated"] == 1 and result["counts"]["removed"] == 0
    assert list(load_documents(fresh)) == ["municipality/N1"]

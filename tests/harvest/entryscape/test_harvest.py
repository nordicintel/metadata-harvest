"""EntryScape: the incremental loop against a fake EntryScape instance."""

import json
from pathlib import Path

from apps.harvest.metadata_adapters.entryscape import parse
from apps.harvest.metadata_adapters.entryscape.harvest import load_tables
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.entryscape import (
    PROVIDERS,
    UUID_A,
    UUID_B,
    UUID_C,
    execute,
    rows,
    source,  # noqa: F401  (fixture)
)

GOLDEN = Path("tests/fixtures/entryscape/accepted")


def table_calls(fake):
    return [c for c in fake.calls if "/rowstore/" in c]


def test_run_stores_tables_and_parse_groups_them_into_the_accepted_collections(
    source,  # noqa: F811
    tmp_path,
):
    from apps.parsing.build import build

    data = tmp_path / "data" / "entryscape"
    result = execute(source, data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["harvested"] == 3 and result["interrupted"] is None
    assert set(load_tables(data)) == {
        f"alpha/{UUID_A}",
        f"alpha/{UUID_B}",
        f"beta/{UUID_C}",
    }
    store = SourceStore(data / "source", "entryscape")
    assert store.get(f"alpha/{UUID_A}/sv")["derived"]["document"]["class"] == "dataset"
    state = lifecycle.load_state(data, "providers")
    assert state["providers"]["alpha"]["complete"] is True
    documents = dict(parse.documents(store, state, PROVIDERS))
    assert {k.split("/")[1] for k in documents} == {"alpha:befolkning", UUID_C}
    built = build(
        "entryscape", tmp_path / "data", tmp_path / "parsed", bindings=PROVIDERS
    )
    for golden in sorted(GOLDEN.glob("*.jsonl.gz")):
        assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
            golden.read_bytes()
        ), golden.name
    assert built["counts"]["parsed"] == 2


def test_unchanged_tables_cost_nothing_and_changes_reharvest(
    source,  # noqa: F811
    tmp_path,
):
    data = tmp_path / "data" / "entryscape"
    execute(source, data, tmp_path / "first")
    source.calls.clear()
    result = execute(source, data, tmp_path / "rerun")
    assert result["counts"]["unchanged"] == 3 and result["counts"]["harvested"] == 0
    assert table_calls(source) == [f"alpha.example/rowstore/dataset/{UUID_B}"]
    source.tables["alpha.example"][UUID_A]["modified"] = "2026-06-01T00:00:00Z"
    source.tables["alpha.example"][UUID_B]["rows"] = rows(45, "B")
    del source.tables["beta.example"][UUID_C]
    result = execute(source, data, tmp_path / "changed")
    assert result["counts"]["harvested"] == 2 and result["counts"]["removed"] == 1
    tables = load_tables(data)
    assert tables[f"alpha/{UUID_B}"]["rowcount"] == 45
    assert f"beta/{UUID_C}" not in tables


def test_budget_stop_keeps_finished_work_and_a_bounded_run_removes_nothing(
    source,  # noqa: F811
    tmp_path,
):
    data = tmp_path / "data" / "entryscape"
    result = execute(source, data, tmp_path / "budget", max_requests=4)
    assert result["interrupted"] and result["status"] == "completed_with_errors"
    assert "budget" in json.dumps(result["errors"])
    assert 0 < result["counts"]["harvested"] < 3
    assert len(load_tables(data)) == result["counts"]["harvested"]
    result = execute(source, data, tmp_path / "continue")
    assert result["status"] == "success" and len(load_tables(data)) == 3
    del source.tables["beta.example"][UUID_C]
    source.calls.clear()
    result = execute(source, data, tmp_path / "bounded", selected=["alpha"])
    assert result["counts"]["removed"] == 0 and f"beta/{UUID_C}" in load_tables(data)
    assert all(call.startswith("alpha.example") for call in source.calls)

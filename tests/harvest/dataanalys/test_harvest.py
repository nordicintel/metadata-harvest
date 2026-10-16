"""Analysverktyg: discovery, one fetch, and a run end to end against recordings."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.dataanalys import parse
from apps.harvest.metadata_adapters.dataanalys.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.dataanalys import BOOTSTRAP, PROVIDER, RecordedClient, execute

GOLDEN = Path("tests/fixtures/dataanalys/accepted")


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_each_area_as_a_scope(tmp_path):
    run = prepared(tmp_path, RecordedClient())
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["GONV", "LRHST", "NRAA"]
    assert run.removals and run.report["areas"] == ["GONV", "LRHST", "NRAA"]
    keys = {t.key for scope in scopes for t in asyncio.run(scope.load())}
    assert keys == BOOTSTRAP
    assert run.store.get_shared("socialstyrelsen_analys", "charts.json")
    assert run.store.get_shared("socialstyrelsen_analys", "catalogue/GONV.json")
    run.selection.update(areas=["gonv", "XYZ"], indicators=["G082", "nothing"])
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["GONV"]
    assert [t.key for t in asyncio.run(scopes[0].load())] == ["sv/GONV|G082"]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [
        ("discovery", "*/XYZ"),
        ("discovery", "*/nothing"),
    ]


def test_fetch_stores_the_cube_with_its_derived_context(tmp_path):
    run = prepared(tmp_path, RecordedClient())
    scope = asyncio.run(ADAPTER.discover(run))[0]
    task = next(t for t in asyncio.run(scope.load()) if t.key == "sv/GONV|G082")
    stored = asyncio.run(ADAPTER.fetch(run, task))
    assert set(stored.responses) == {"cube"}
    assert set(stored.derived) == {"row", "columns", "area", "link", "subject_label"}
    assert stored.derived["link"]["resolution"] != "unresolved"
    assert stored.updated and stored.info == {"mapping_version": 1}
    assert run.report["out_of_scope"] == []


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    data = tmp_path / "data" / "dataanalys"
    result = execute(RecordedClient(), data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 7 and result["published"] == {"sv": 7}
    store = SourceStore(data / "source", "dataanalys")
    state = lifecycle.load_state(data, "datasets")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert len(documents) == 7 and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build(
        "dataanalys", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER]
    )
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == 7
    again = execute(RecordedClient(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == 7 and again["counts"]["rebuilt"] == 0

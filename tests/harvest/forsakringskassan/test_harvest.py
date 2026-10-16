"""Försäkringskassan: discovery, one fetch, and a run end to end against recordings."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.forsakringskassan import parse
from apps.harvest.metadata_adapters.forsakringskassan.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from harvest.lifecycle import Kept
from tests.support.forsakringskassan import (
    ALL_KEYS,
    PROVIDER,
    RecordedClient,
    execute,
)

GOLDEN = Path("tests/fixtures/forsakringskassan/accepted")


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_each_report_as_a_scope(tmp_path):
    run = prepared(tmp_path, RecordedClient())
    scopes = asyncio.run(ADAPTER.discover(run))
    assert {s.name for s in scopes} >= {"ase-avslutsanledning", "ase-beslut-lan"}
    keys = {t.key for scope in scopes for t in asyncio.run(scope.load())}
    assert keys == ALL_KEYS | {"sv/syn-kommun/SynKommunLan"}  # excluded at fetch
    assert run.store.get_shared("forsakringskassan", "texts.json")
    assert run.store.get_shared("forsakringskassan", "meta/ase-beslut-lan.json")
    run.selection["subjects"] = ["ase-beslut-lan", "nothing"]
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["ase-beslut-lan"]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [
        ("discovery", "*/nothing")
    ]


def test_fetch_stores_the_listing_row_and_probe_then_keeps_the_report(tmp_path):
    source = RecordedClient()
    run = prepared(tmp_path, source)
    scope = next(
        s for s in asyncio.run(ADAPTER.discover(run)) if s.name == "ase-beslut-lan"
    )
    tasks = asyncio.run(scope.load())
    stored = {t.key: asyncio.run(ADAPTER.fetch(run, t)) for t in tasks}
    assert all(set(s.responses) >= {"listing"} for s in stored.values())
    assert all(s.derived["table"]["name"] for s in stored.values())
    for key, item in stored.items():
        run.store.put(
            run.stored_key(key),
            responses=item.responses,
            derived=item.derived,
            updated=None,
            retrieved_at=run.stamp,
        )
    run.state["reports"]["ase-beslut-lan"]["complete"] = True
    tasks = asyncio.run(scope.load())
    assert all(isinstance(asyncio.run(ADAPTER.fetch(run, t)), Kept) for t in tasks)
    run.selection["full"] = True
    tasks = asyncio.run(scope.load())
    assert not any(isinstance(asyncio.run(ADAPTER.fetch(run, t)), Kept) for t in tasks)


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    data = tmp_path / "data" / "forsakringskassan"
    result = execute(RecordedClient(), data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 5 and result["published"] == {"sv": 5}
    store = SourceStore(data / "source", "forsakringskassan")
    state = lifecycle.load_state(data, "datasets")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert set(documents) == {
        f"forsakringskassan/{k.split('/', 1)[1]}/sv" for k in ALL_KEYS
    }
    assert not any(isinstance(d, Exception) for d in documents.values())
    built = build(
        "forsakringskassan", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER]
    )
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == 5
    again = execute(RecordedClient(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == 5 and again["counts"]["rebuilt"] == 0
    full = execute(RecordedClient(), data, tmp_path / "third", full=True)
    assert full["counts"]["unchanged"] == 5 and full["counts"]["rebuilt"] == 0

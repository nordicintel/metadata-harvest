"""Tillväxtverket: discovery, one fetch, and a run end to end against recordings."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.tillvaxtverket import parse, source
from apps.harvest.metadata_adapters.tillvaxtverket.harvest import ADAPTER
from apps.harvest.metadata_adapters.tillvaxtverket.mapping import MAPPING_VERSION
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from harvest.lifecycle import Kept
from tests.support.tillvaxtverket import (
    NAMES,
    PROVIDER,
    Source,
    distinct_key,
    execute,
    labels_key,
    probe_key,
    snapshot_from,
)

GOLDEN = Path("tests/fixtures/tillvaxtverket/accepted")
YEAR = "GuestNights_Country_Year"


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_the_registry_without_requests(tmp_path):
    client = Source()
    run = prepared(tmp_path, client)
    [scope] = asyncio.run(ADAPTER.discover(run))
    tasks = asyncio.run(scope.load())
    assert [t.key for t in tasks] == [f"sv/{s.dataset_code}" for s in source.DATASETS]
    assert client.requests == []
    run.selection["products"] = [YEAR.lower(), "Nope"]
    [scope] = asyncio.run(ADAPTER.discover(run))
    assert [t.item.name for t in asyncio.run(scope.load())] == [YEAR]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [
        ("discovery", "*/Nope")
    ]


def test_fetch_stores_the_probe_cube_and_label_file_then_keeps_it(tmp_path):
    client = Source()
    run = prepared(tmp_path, client)
    task = next(
        t
        for t in asyncio.run(asyncio.run(ADAPTER.discover(run))[0].load())
        if t.item.name == YEAR
    )
    stored = asyncio.run(ADAPTER.fetch(run, task))
    assert set(stored.responses) == {
        "columns",
        "load_dates",
        "regions",
        "dimensions",
        "periods",
    }
    assert stored.files == {parse.LABELS: client.responses[labels_key(YEAR)]}
    assert stored.info == {"mapping_version": MAPPING_VERSION, "rebuild": False}
    assert client.requests[:2] == [probe_key(YEAR), distinct_key(YEAR, "LOAD_DATE")]
    run.store.put(
        run.stored_key(task.key),
        responses=stored.responses,
        updated=stored.updated,
        retrieved_at=run.stamp,
        files=stored.files,
    )
    run.records[task.key] = {**stored.info, "verified_at": run.stamp}
    client.requests.clear()
    assert isinstance(asyncio.run(ADAPTER.fetch(run, task)), Kept)
    assert client.requests == [probe_key(YEAR), distinct_key(YEAR, "LOAD_DATE")]


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    data = tmp_path / "data" / "tillvaxtverket"
    result = execute(Source(), data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 6 and result["published"] == {"sv": 6}
    store = SourceStore(data / "source", "tillvaxtverket")
    state = lifecycle.load_state(data, "datasets")
    portal = snapshot_from(tmp_path / "data" / "data_eu", NAMES)
    documents = dict(parse.documents(store, state, [PROVIDER], portal=portal))
    assert len(documents) == 6 and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build(
        "tillvaxtverket",
        tmp_path / "data",
        tmp_path / "parsed",
        bindings=[PROVIDER],
        portal=portal,
    )
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == 6
    again = execute(Source(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == 6 and again["counts"]["rebuilt"] == 0

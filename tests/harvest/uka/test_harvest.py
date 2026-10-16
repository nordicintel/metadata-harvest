"""UKÄ: discovery, one fetch, and a run end to end against recorded indicators."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.uka import parse
from apps.harvest.metadata_adapters.uka.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from harvest.lifecycle import Kept
from tests.support.uka import PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/uka/accepted")


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_the_catalogue_and_reports_unknown_indicators(tmp_path):
    run = prepared(tmp_path, Source())
    [scope] = asyncio.run(ADAPTER.discover(run))
    tasks = asyncio.run(scope.load())
    assert [t.key for t in tasks] == ["sv/67", "sv/72"] and scope.complete
    assert run.store.get_shared("uka", "indicators.json")
    run.selection["indicators"] = ["72", "999"]
    [scope] = asyncio.run(ADAPTER.discover(run))
    assert [t.key for t in asyncio.run(scope.load())] == ["sv/72"]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [("discovery", "*/999")]


def test_fetch_scans_exports_then_keeps_the_entry_after_a_head_check(tmp_path):
    source = Source()
    run = prepared(tmp_path, source)
    task = asyncio.run(asyncio.run(ADAPTER.discover(run))[0].load())[0]
    stored = asyncio.run(ADAPTER.fetch(run, task))
    assert set(stored.responses) == {"catalogue", "totals", "links"}
    assert stored.derived["scan"]["files"] and source.downloads
    assert set(stored.info) == {
        "files",
        "source_digest",
        "mapping_version",
        "skipped_periods",
    }
    run.store.put(
        run.stored_key(task.key),
        responses=stored.responses,
        derived=stored.derived,
        updated=None,
        retrieved_at=run.stamp,
    )
    run.records[task.key] = {**stored.info, "verified_at": run.stamp}
    source.downloads.clear()
    kept = asyncio.run(ADAPTER.fetch(run, task))
    assert isinstance(kept, Kept) and source.downloads == [] and source.heads
    run.selection["full"] = True
    assert not isinstance(asyncio.run(ADAPTER.fetch(run, task)), Kept)


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    data = tmp_path / "data" / "uka"
    result = execute(Source(), data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 2 and result["published"] == {"sv": 2}
    store = SourceStore(data / "source", "uka")
    state = lifecycle.load_state(data, "indicators")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert len(documents) == 2 and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build("uka", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER])
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == 2
    again = execute(Source(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == 2 and again["counts"]["rebuilt"] == 0

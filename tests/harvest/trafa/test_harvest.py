"""Trafikanalys: discovery, one fetch, and a run end to end against recordings."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.trafa import parse
from apps.harvest.metadata_adapters.trafa.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.trafa import PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/trafa/accepted")


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_each_product_as_a_scope(tmp_path):
    run = prepared(tmp_path, Source())
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["T10011", "T10033", "T04010"]
    assert run.report["excluded"] == {"ATFT": ADAPTER_EXCLUDED}
    tasks = asyncio.run(scopes[0].load())
    assert tasks and all(t.key.startswith("sv/T10011|") for t in tasks)
    assert run.store.get_shared("trafa", "structure.T10011.sv.json")
    assert run.store.get_shared("trafa", "documentation.T10011.html")
    run.selection["products"] = ["t10033", "Nope"]
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["T10033"]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [
        ("discovery", "*/Nope")
    ]


ADAPTER_EXCLUDED = 'Test product "Annotering test", harvested only on request.'


def test_fetch_stores_the_swedish_probe(tmp_path):
    source = Source()
    run = prepared(tmp_path, source)
    task = asyncio.run(asyncio.run(ADAPTER.discover(run))[0].load())[0]
    source.requests.clear()
    stored = asyncio.run(ADAPTER.fetch(run, task))
    assert set(stored.responses) == {"probe"} and "Header" in stored.responses["probe"]
    assert len(source.requests) == 1
    assert asyncio.run(ADAPTER.fetch(run, task)).responses == stored.responses
    assert len(source.requests) == 1  # the probe is cached within the product


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    data = tmp_path / "data" / "trafa"
    result = execute(Source(), data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == result["published"]["sv"] > 0
    store = SourceStore(data / "source", "trafa")
    state = lifecycle.load_state(data, "datasets")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert len(documents) == result["counts"]["rebuilt"] and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build("trafa", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER])
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == len(documents)
    again = execute(Source(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == len(documents)
    assert again["counts"]["rebuilt"] == 0

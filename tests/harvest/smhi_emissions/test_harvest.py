"""SMHI emissions: discovery, one fetch, and a run end to end against recordings."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.smhi_emissions import parse
from apps.harvest.metadata_adapters.smhi_emissions.client import FILTER_OPTIONS_URL
from apps.harvest.metadata_adapters.smhi_emissions.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.smhi_emissions import (
    PROVIDER,
    Source,
    execute,
    fixture_key,
    probe_key,
    substances,
)

GOLDEN = Path("tests/fixtures/smhi_emissions/accepted")


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_every_substance_once_and_reports_unknown_subjects(tmp_path):
    run = prepared(tmp_path, Source())
    run.selection["subjects"] = ["co2", "XYZ"]
    [scope] = asyncio.run(ADAPTER.discover(run))
    tasks = asyncio.run(scope.load())
    assert [t.key for t in tasks] == ["sv/emissionsdatabasen_CO2"]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [("discovery", "*/XYZ")]
    assert run.store.get_shared("smhi", "filteroptions.json")
    run.selection["subjects"] = None
    [scope] = asyncio.run(ADAPTER.discover(run))
    assert len(asyncio.run(scope.load())) == len(substances()) == 29


def test_fetch_stores_the_probe_with_its_submission_round(tmp_path):
    run = prepared(tmp_path, Source())
    [scope] = asyncio.run(ADAPTER.discover(run))
    task = next(t for t in asyncio.run(scope.load()) if t.key.endswith("_CO2"))
    stored = asyncio.run(ADAPTER.fetch(run, task))
    assert set(stored.responses) == {"getdata"} and stored.updated == 2026
    assert stored.derived["substance"]["code"] == "CO2"
    assert stored.info["submission"] == 2026 and run.report["submissions"] == [2026]


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    source = Source()
    data = tmp_path / "data" / "smhi_emissions"
    result = execute(source, data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 29 and result["tree_complete"] is True
    assert source.requests == [
        fixture_key(FILTER_OPTIONS_URL),
        *(probe_key(code) for code in substances()),
    ]
    store = SourceStore(data / "source", "smhi_emissions")
    state = lifecycle.load_state(data, "datasets")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert len(documents) == 29 and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build(
        "smhi_emissions", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER]
    )
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (
        tmp_path / "parsed/metadata" / golden.name
    ).read_bytes() == golden.read_bytes()
    again = execute(Source(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == 29 and again["counts"]["rebuilt"] == 0
    assert built["counts"]["parsed"] == 29

"""Riksbank: discovery, one fetch, and a run end to end against recordings."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.riksbank import parse
from apps.harvest.metadata_adapters.riksbank.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.riksbank import ALL, PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/riksbank/accepted")


def prepared(tmp_path, source):
    return lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=source
    )


def test_discover_lists_each_api_as_a_scope(tmp_path):
    run = prepared(tmp_path, Source())
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["swea", "mpd", "swestr"]
    keys = {t.key for scope in scopes for t in asyncio.run(scope.load())}
    assert keys == ALL
    assert run.store.get_shared("riksbank", "swea_groups.json")
    assert run.store.get_shared("riksbank", "mpd_policy_rounds.json")
    run.selection["subjects"] = ["2", "SEQGDPNAYSA", "nothing"]
    scopes = asyncio.run(ADAPTER.discover(run))
    assert [s.name for s in scopes] == ["swea", "mpd"]
    keys = [t.key for scope in scopes for t in asyncio.run(scope.load())]
    assert keys == ["en/swea_group_2", "en/mpd_SEQGDPNAYSA"]
    assert [(e["stage"], e["resource"]) for e in run.errors] == [
        ("discovery", "*/nothing")
    ]


def test_fetch_stores_the_probe_with_the_record_and_its_latest_date(tmp_path):
    run = prepared(tmp_path, Source())
    tasks = {
        t.key: t
        for scope in asyncio.run(ADAPTER.discover(run))
        for t in asyncio.run(scope.load())
    }
    swea = asyncio.run(ADAPTER.fetch(run, tasks["en/swea_group_2"]))
    assert set(swea.responses) == {"latest"} and swea.updated
    assert swea.derived["record"]["api"] == "swea"
    mpd = asyncio.run(ADAPTER.fetch(run, tasks["en/mpd_SEQGDPNAYSA"]))
    assert set(mpd.responses) == {"vintage"} and mpd.updated.startswith("20")
    swestr = asyncio.run(ADAPTER.fetch(run, tasks["en/swestr"]))
    assert set(swestr.responses) == {"latest"}


def test_run_collects_source_files_that_parse_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    data = tmp_path / "data" / "riksbank"
    result = execute(Source(), data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(ALL) and result["published"] == {
        "en": len(ALL)
    }
    store = SourceStore(data / "source", "riksbank")
    state = lifecycle.load_state(data, "datasets")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert len(documents) == len(ALL) and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build(
        "riksbank", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER]
    )
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == len(ALL)
    again = execute(Source(), data, tmp_path / "second")
    assert again["counts"]["unchanged"] == len(ALL)
    assert again["counts"]["rebuilt"] == 0

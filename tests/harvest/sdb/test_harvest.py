"""SDB: discovery, one fetch, and a run end to end against the SDB imitation."""

import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.sdb import parse
from apps.harvest.metadata_adapters.sdb.client import SdbClient
from apps.harvest.metadata_adapters.sdb.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.sdb import PROVIDER, execute, seeded  # noqa: F401  (fixture)

GOLDEN = Path("tests/fixtures/sdb/accepted")


def prepared(tmp_path, sdb):
    client = SdbClient(base_url=sdb.api_root, retry_delay=0)
    run = lifecycle.prepare(
        ADAPTER, PROVIDER, tmp_path / "data", tmp_path / "out", client=client
    )
    return run, client


def test_discover_lists_each_language_and_reports_unknown_subjects(
    seeded,  # noqa: F811
    tmp_path,
):
    async def main():
        run, client = prepared(tmp_path, seeded)
        try:
            run.selection["subjects"] = ["amning", "nothing"]
            scopes = await ADAPTER.discover(run)
            assert [s.name for s in scopes] == ["sv", "en"]
            sv = await scopes[0].load()
            en = await scopes[1].load()
            assert [t.key for t in sv] == ["sv/amning"] and en == []
            assert [(e["stage"], e["resource"]) for e in run.errors] == [
                ("discovery", "*/nothing")
            ]
            assert run.store.get_shared("socialstyrelsen", "listing.sv.json")
            run.selection["subjects"] = None
            scopes = await ADAPTER.discover(run)
            assert len(await scopes[0].load()) == 3
            assert [t.key for t in await scopes[1].load()] == ["en/dodsorsaker"]
        finally:
            await client.close()

    asyncio.run(main())


def test_fetch_stores_the_responses_and_shares_the_probe_once(
    seeded,  # noqa: F811
    tmp_path,
):
    async def main():
        run, client = prepared(tmp_path, seeded)
        try:
            scopes = await ADAPTER.discover(run)
            sv = {t.key: t for t in await scopes[0].load()}
            en = {t.key: t for t in await scopes[1].load()}
            stored = await ADAPTER.fetch(run, sv["sv/dodsorsaker"])
            assert set(stored.responses) == {
                "variables",
                "categories",
                "resultat",
                "listing_row",
            }
            probe = run.store.get_shared("socialstyrelsen", "probe/dodsorsaker.json")
            assert probe["language"] == "sv" and probe["first"]["data"]
            await ADAPTER.fetch(run, en["en/dodsorsaker"])
            assert (
                run.store.get_shared("socialstyrelsen", "probe/dodsorsaker.json")
                == probe
            )
        finally:
            await client.close()

    asyncio.run(main())


def test_run_collects_source_files_that_parse_to_the_accepted_collection(
    seeded,  # noqa: F811
    tmp_path,
):
    from apps.parsing.build import build

    data = tmp_path / "data" / "sdb"
    result = execute(seeded, data, tmp_path / "first")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 4 and result["published"] == {
        "sv": 3,
        "en": 1,
    }
    store = SourceStore(data / "source", "sdb")
    state = lifecycle.load_state(data, "subjects")
    assert parse.withheld_keys(state) == set()
    documents = dict(parse.documents(store, state, [PROVIDER]))
    assert len(documents) == 4 and not any(
        isinstance(d, Exception) for d in documents.values()
    )
    built = build("sdb", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER])
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == 4
    again = execute(seeded, data, tmp_path / "second")
    assert again["counts"]["unchanged"] == 4 and again["counts"]["rebuilt"] == 0

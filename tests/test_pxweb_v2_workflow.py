"""Real filesystem lifecycle tests with HTTP fixtures at the source boundary."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from metadata_harvest.storage import JsonlStore, read
from tests.test_pxweb_v2 import CONFIG, PROVIDER, client_for, codelist, metadata, table


class Source:
    def __init__(self):
        self.tables = {"sv": [table()], "en": [table("EN1")]}
        self.requests = []
        self.fail_languages = set()
        self.fail_metadata = set()
        self.fail_codes = set()
        self.updated = None
        self.label = "Native metadata title"

    def handle(self, request):
        path, params = request.url.path, request.url.params
        language = params.get("lang")
        self.requests.append((path, language))
        if path.endswith("/config"):
            return httpx.Response(200, json=CONFIG)
        if path.endswith("/tables"):
            if language in self.fail_languages:
                return httpx.Response(503)
            rows = self.tables[language]
            return httpx.Response(
                200,
                json={
                    "language": language,
                    "tables": rows,
                    "page": {
                        "pageNumber": 1,
                        "pageSize": 1000,
                        "totalElements": len(rows),
                        "totalPages": 1,
                    },
                },
            )
        if "/codelists/" in path:
            return (
                httpx.Response(503)
                if language in self.fail_codes
                else httpx.Response(200, json=codelist(language))
            )
        code = path.split("/")[-2]
        if code in self.fail_metadata:
            return httpx.Response(503)
        payload = metadata(code, language)
        payload["label"] = self.label
        payload["updated"] = self.updated or payload["updated"]
        return httpx.Response(200, json=payload)


def harvest(source, root, output, **kwargs):
    from metadata_harvest.adapters.pxweb_v2.workflow import collect, run

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await run(PROVIDER, root, output, client=client, **kwargs)

    result = asyncio.run(execute())
    collect(root, output, result["run_id"], result["wave"])
    return result


def metadata_bytes(root):
    return {p.name: p.read_bytes() for p in (root / "metadata").glob("*.jsonl")}


def test_full_incremental_and_silent_change_refresh(tmp_path):
    source, root = Source(), tmp_path / "data"
    initial = harvest(source, root, tmp_path / "first", full=True)
    assert initial["status"] == "success"
    assert initial["counts"]["created"] == 2
    assert len(list((root / "metadata").glob("*.jsonl"))) == 2
    assert len(list((root / "codelists").glob("*.jsonl"))) == 2
    assert not (root / "work/current.json").exists()
    before = metadata_bytes(root)
    source.requests.clear()
    unchanged = harvest(source, root, tmp_path / "second")
    assert unchanged["counts"]["reused"] == 2
    assert not any(
        path.endswith("/metadata") or "/codelists/" in path
        for path, _ in source.requests
    )
    assert metadata_bytes(root) == before
    source.label = "Corrected upstream without timestamp"
    full = harvest(source, root, tmp_path / "third", full=True)
    assert full["counts"]["updated"] == 2
    assert {doc["label"] for doc in JsonlStore(root / "metadata").records.values()} == {
        source.label
    }
    assert sorted(p.name for p in (root / "reports").iterdir()) == [
        "harvest.json",
        "refresh.json",
    ]


def test_removal_and_partial_discovery_are_language_isolated(tmp_path):
    source, root = Source(), tmp_path / "v2"
    sibling = tmp_path / "v1"
    sibling.mkdir()
    (sibling / "sentinel").write_text("unchanged")
    harvest(source, root, tmp_path / "first")
    source.tables["sv"] = []
    source.fail_languages.add("en")
    result = harvest(source, root, tmp_path / "second")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["removed"] == 1
    assert [
        doc["extension"]["px"]["language"]
        for doc in JsonlStore(root / "metadata").records.values()
    ] == ["en"]
    assert len(list((root / "codelists").glob("*.jsonl"))) == 1
    assert (sibling / "sentinel").read_text() == "unchanged"


@pytest.mark.parametrize("failure", ["metadata", "codelist"])
def test_failed_refresh_preserves_previous_content_and_retries_next_run(
    tmp_path, failure
):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    before = metadata_bytes(root)
    if failure == "metadata":
        source.fail_metadata.add("T1")
    else:
        source.fail_codes.add("sv")
    result = harvest(source, root, tmp_path / "second", full=True)
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["failed"] == 1
    assert metadata_bytes(root) == before
    source.fail_metadata.clear()
    source.fail_codes.clear()
    source.requests.clear()
    result = harvest(source, root, tmp_path / "third")
    assert result["status"] == "success"
    assert ("/api/v2/tables/T1/metadata", "sv") in source.requests


def test_shared_codelist_is_fetched_once_for_multiple_tables(tmp_path):
    source = Source()
    source.tables["sv"].append(table("T2"))
    result = harvest(source, tmp_path / "data", tmp_path / "output")
    assert result["counts"]["created"] == 3
    assert source.requests.count(("/api/v2/codelists/vs_region", "sv")) == 1


def test_continuation_retains_full_mode_and_rejects_stale_artifact(tmp_path):
    from metadata_harvest.adapters.pxweb_v2.workflow import collect

    source, root = Source(), tmp_path / "data"
    first = harvest(source, root, tmp_path / "first", full=True, budget_seconds=0)
    assert first["continue"] is True
    assert read(root / "work/current.json")["full"] is True
    state = deepcopy(read(root / "work/current.json"))
    with pytest.raises(ValueError, match="stale|Stale"):
        collect(root, tmp_path / "first", first["run_id"], first["wave"])
    assert read(root / "work/current.json") == state
    second = harvest(source, root, tmp_path / "second")
    assert second["run_id"] == first["run_id"]
    assert second["full"] is True
    assert second["status"] == "success"
    assert not (root / "work/current.json").exists()


def test_collector_rejects_tampered_metadata_without_writing(tmp_path):
    from metadata_harvest.adapters.pxweb_v2.workflow import collect, run

    source, root, output = Source(), tmp_path / "data", tmp_path / "output"

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await run(PROVIDER, root, output, client=client)

    result = asyncio.run(execute())
    target = next((output / result["snapshot"] / "metadata").glob("*.jsonl"))
    target.write_text("{}\n")
    with pytest.raises(ValueError):
        collect(root, output, result["run_id"], result["wave"])
    assert not root.exists()


def test_continuation_refetches_codelist_pruned_after_failed_new_table(
    tmp_path, monkeypatch
):
    from metadata_harvest.adapters.pxweb_v2 import workflow

    source, root = Source(), tmp_path / "data"
    source.tables["sv"].append(table("T2"))
    original = source.handle

    def handler(request):
        if request.url.path.endswith("/codelists/zz_missing"):
            return httpx.Response(503)
        response = original(request)
        if request.url.path.endswith("/tables/T1/metadata"):
            payload = response.json()
            payload["dimension"]["Region"]["extension"]["codelists"].append(
                {"id": "zz_missing"}
            )
            return httpx.Response(200, json=payload)
        return response

    source.handle = handler
    ticks = iter([0, 0, 0, 0])
    with monkeypatch.context() as patch:
        patch.setattr(
            workflow, "time", SimpleNamespace(monotonic=lambda: next(ticks, 1000))
        )
        first = harvest(source, root, tmp_path / "first", budget_seconds=200)
    assert first["counts"]["failed"] == 1
    assert first["counts"]["unfinished"] == 2
    second = harvest(source, root, tmp_path / "second")
    assert second["counts"]["created"] == 2
    assert source.requests.count(("/api/v2/codelists/vs_region", "sv")) == 2


def test_confirmed_removed_codelist_payload_is_deleted_and_reported(tmp_path):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    original = source.handle

    def handler(request):
        if "/codelists/" in request.url.path and request.url.params["lang"] == "sv":
            return httpx.Response(404)
        return original(request)

    source.handle = handler
    result = harvest(source, root, tmp_path / "second", full=True)
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["failed"] == 1
    codes = JsonlStore(
        root / "codelists",
        identify=lambda d: {
            "provider_code": "scb",
            "dataset_code": d["codelist"]["id"],
            "language": d["language"],
        },
    )
    assert [d["language"] for d in codes.records.values()] == ["en"]


def test_collector_rejects_unsupported_language_deletion_even_with_valid_manifest(
    tmp_path,
):
    from metadata_harvest.adapters.pxweb_v2 import workflow
    from metadata_harvest.storage import write

    source, root, output = Source(), tmp_path / "data", tmp_path / "second"
    harvest(source, root, tmp_path / "first")
    before = workflow.manifest(root)
    source.fail_languages.add("en")

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await workflow.run(PROVIDER, root, output, client=client)

    result = asyncio.run(execute())
    candidate = output / result.get("snapshot", "")
    for folder in ("metadata", "codelists", "table-records"):
        next((candidate / folder).glob("*__en__*.jsonl")).unlink()
    result["manifest"] = workflow.manifest(candidate)
    write(output / "result.json", result)
    with pytest.raises(ValueError, match="remov|scope|listing"):
        workflow.collect(root, output, result["run_id"], result["wave"])
    assert workflow.manifest(root) == before


def test_interrupted_checkpoint_leaves_previous_certified_candidate_collectable(
    tmp_path, monkeypatch
):
    from metadata_harvest.adapters.pxweb_v2 import workflow

    source, root, output = Source(), tmp_path / "data", tmp_path / "output"
    original = workflow._snapshot
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(*args, **kwargs)
        original_write = workflow.write

        def fail_write(path, value):
            if Path(path).name == "harvest.json":
                raise OSError("Simulated interruption during checkpoint")
            return original_write(path, value)

        monkeypatch.setattr(workflow, "write", fail_write)
        return original(*args, **kwargs)

    from pathlib import Path

    ticks = iter([0, 0, 0, 61, 61, 61])
    with monkeypatch.context() as patch:
        patch.setattr(
            workflow, "time", SimpleNamespace(monotonic=lambda: next(ticks, 62))
        )
        patch.setattr(workflow, "_snapshot", interrupted)

        async def execute():
            client, _ = client_for(source.handle)
            async with client:
                return await workflow.run(PROVIDER, root, output, client=client)

        with pytest.raises(OSError, match="Simulated"):
            asyncio.run(execute())
    # Restore the write boundary before collecting the previously certified batch.
    from metadata_harvest.storage import write

    monkeypatch.setattr(workflow, "write", write)
    result = read(output / "result.json")
    accepted = workflow.collect(root, output, result["run_id"], result["wave"])
    assert accepted["counts"]["created"] == 1
    assert accepted["counts"]["unfinished"] == 1

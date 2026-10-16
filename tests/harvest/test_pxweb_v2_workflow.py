"""Real filesystem lifecycle tests with HTTP fixtures at the source boundary."""

import asyncio
import json

import httpx
import pytest

from apps.harvest.source_store import SourceStore
from apps.harvest.storage import JsonlStore, read
from tests.harvest.test_pxweb_v2 import (
    CONFIG,
    PROVIDER,
    client_for,
    codelist,
    metadata,
    table,
)
from tests.support.parsing import documents


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
    from apps.harvest.metadata_adapters.pxweb_v2.workflow import collect, run

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await run(PROVIDER, root, output, client=client, **kwargs)

    result = asyncio.run(execute())
    collect(root, output, result["run_id"])
    return result


def metadata_bytes(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in (root / "source").rglob("*.json")
    }


def parsed(root):
    return documents("pxweb_v2", root, [PROVIDER])


def store(root):
    return SourceStore(root / "source", "pxweb_v2")


def test_live_progress_exposes_individual_errors_before_harvest_finishes(
    tmp_path, monkeypatch
):
    source = Source()
    source.fail_codes.add("sv")
    source.fail_metadata.add("EN1")
    path = tmp_path / "telemetry/progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(path))
    handle = source.handle
    observed = []

    def inspect(request):
        if request.url.params.get("lang") == "en" and request.url.path.endswith(
            "/metadata"
        ):
            observed.append(read(path))
            errors = [
                json.loads(line)
                for line in path.with_suffix(".errors.jsonl").read_text().splitlines()
            ]
            assert errors[0]["resource"] == "vs_region"
            assert errors[0]["language"] == "sv"
            assert errors[0]["stage"] == "codelist"
            assert "503" in errors[0]["error"]
        return handle(request)

    source.handle = inspect
    result = harvest(source, tmp_path / "data", tmp_path / "artifact")
    assert observed[0]["processed"] == 1
    assert observed[0]["succeeded"] == 1
    assert observed[0]["failed"] == 0
    assert observed[0]["remaining"] == 1
    assert observed[0]["errors_by_stage"] == {"codelist": 1}
    assert read(path)["failed"] == result["counts"]["failed"] == 1
    assert read(path)["succeeded"] == 1
    assert read(path)["error_count"] == 2
    assert not any("telemetry" in name for name in result["manifest"])


def test_full_incremental_and_silent_change_refresh(tmp_path):
    source, root = Source(), tmp_path / "data"
    initial = harvest(source, root, tmp_path / "first", full=True)
    assert initial["status"] == "success"
    assert initial["counts"]["created"] == 2
    assert set(store(root).entries()) == {"scb/T1/sv", "scb/EN1/en"}
    assert set(store(root).get("scb/T1/sv")["responses"]) == {"metadata", "table"}
    assert store(root).get("scb/T1/sv")["updated"] == table()["updated"]
    assert not (root / "metadata").exists()
    assert {
        doc["extension"]["nordicintel"]["language"] for doc in parsed(root).values()
    } == {"sv", "en"}
    assert len(list((root / "codelists").glob("*.jsonl.gz"))) == 2
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
    assert {doc["label"] for doc in parsed(root).values()} == {source.label}
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
        doc["extension"]["nordicintel"]["language"] for doc in parsed(root).values()
    ] == ["en"]
    assert list(store(root).entries()) == ["scb/EN1/en"]
    assert len(list((root / "codelists").glob("*.jsonl.gz"))) == 1
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
    assert result["counts"]["failed"] == (1 if failure == "metadata" else 0)
    assert result["counts"]["codelists_failed"] == (1 if failure == "codelist" else 0)
    assert metadata_bytes(root) == before
    source.fail_metadata.clear()
    source.fail_codes.clear()
    source.requests.clear()
    result = harvest(source, root, tmp_path / "third")
    assert result["status"] == "success"
    assert (("/api/v2/tables/T1/metadata", "sv") in source.requests) == (
        failure == "metadata"
    )
    if failure == "codelist":
        assert ("/api/v2/codelists/vs_region", "sv") in source.requests


def test_shared_codelist_is_fetched_once_for_multiple_tables(tmp_path):
    source = Source()
    source.tables["sv"].append(table("T2"))
    result = harvest(source, tmp_path / "data", tmp_path / "output")
    assert result["counts"]["created"] == 3
    assert source.requests.count(("/api/v2/codelists/vs_region", "sv")) == 1


def test_codelist_failure_accepts_table_and_retries_only_dependency(tmp_path):
    source, root = Source(), tmp_path / "data"
    source.fail_codes.add("sv")
    result = harvest(source, root, tmp_path / "first")
    assert result["counts"]["created"] == 2
    assert result["counts"]["failed"] == 0
    assert result["counts"]["codelists_failed"] == 1
    assert len(parsed(root)) == 2
    assert result["errors"][0]["stage"] == "codelist"
    before = metadata_bytes(root)
    source.fail_codes.clear()
    source.requests.clear()
    retried = harvest(source, root, tmp_path / "second")
    assert retried["status"] == "success"
    assert retried["counts"]["reused"] == 2
    assert not any(path.endswith("/metadata") for path, _ in source.requests)
    assert ("/api/v2/codelists/vs_region", "sv") in source.requests
    assert metadata_bytes(root) == before


def test_failed_codelist_does_not_block_changed_table_metadata(tmp_path):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    source.label = "New accepted source title"
    source.fail_codes.add("sv")
    result = harvest(source, root, tmp_path / "second", full=True)
    assert result["counts"]["updated"] == 2
    assert result["counts"]["failed"] == 0
    assert result["counts"]["codelists_failed"] == 1
    assert {doc["label"] for doc in parsed(root).values()} == {source.label}


def test_dependency_retry_reuses_other_valid_codelists(tmp_path):
    source, root = Source(), tmp_path / "data"
    original = source.handle
    broken = True

    def handler(request):
        if request.url.path.endswith("/codelists/zz_missing"):
            if broken:
                return httpx.Response(503)
            payload = codelist(request.url.params["lang"])
            payload["id"] = "zz_missing"
            return httpx.Response(200, json=payload)
        response = original(request)
        if request.url.path.endswith("/tables/T1/metadata"):
            payload = response.json()
            payload["dimension"]["Region"]["extension"]["codelists"].append(
                {"id": "zz_missing"}
            )
            return httpx.Response(200, json=payload)
        return response

    source.handle = handler
    harvest(source, root, tmp_path / "first")
    broken = False
    source.requests.clear()
    result = harvest(source, root, tmp_path / "second")
    assert result["status"] == "success"
    assert result["counts"]["codelists_fetched"] == 1
    assert not any(
        "/codelists/" in path or path.endswith("/metadata")
        for path, _ in source.requests
    )


def test_metadata_failure_still_retries_missing_dependency_of_retained_table(tmp_path):
    source, root = Source(), tmp_path / "data"
    source.fail_codes.add("sv")
    harvest(source, root, tmp_path / "first")
    source.fail_metadata.add("T1")
    result = harvest(source, root, tmp_path / "second", full=True)
    assert result["counts"]["failed"] == 1
    assert result["counts"]["codelists_failed"] == 1
    assert len(parsed(root)) == 2
    source.fail_codes.clear()
    result = harvest(source, root, tmp_path / "third", full=True)
    assert result["counts"]["failed"] == 1
    assert result["counts"]["codelists_failed"] == 0


def test_incomplete_discovery_keeps_unresolved_dependency_visible(tmp_path):
    source, root = Source(), tmp_path / "data"
    source.fail_codes.add("sv")
    harvest(source, root, tmp_path / "first")
    source.fail_languages.add("sv")
    result = harvest(source, root, tmp_path / "second")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["codelists_failed"] == 1
    assert len(parsed(root)) == 2


@pytest.mark.parametrize("resource", ["metadata", "codelist"])
def test_nonfinite_source_json_cannot_poison_other_tables(tmp_path, resource):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    original = source.handle

    def handler(request):
        response = original(request)
        if request.url.params.get("lang") == "sv" and (
            request.url.path.endswith("/metadata")
            if resource == "metadata"
            else "/codelists/" in request.url.path
        ):
            payload = response.json()
            payload["extraSourceField"] = float("nan")
            return httpx.Response(200, content=json.dumps(payload))
        return response

    source.handle = handler
    before = metadata_bytes(root)
    result = harvest(source, root, tmp_path / "second", full=True)
    assert result["counts"]["failed"] == (1 if resource == "metadata" else 0)
    assert result["counts"]["codelists_failed"] == (1 if resource == "codelist" else 0)
    assert result["counts"]["updated"] == (1 if resource == "metadata" else 2)
    assert metadata_bytes(root) == before


def test_collector_rejects_tampered_metadata_without_writing(tmp_path):
    from apps.harvest.metadata_adapters.pxweb_v2.workflow import collect, run

    source, root, output = Source(), tmp_path / "data", tmp_path / "output"

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await run(PROVIDER, root, output, client=client)

    result = asyncio.run(execute())
    target = next((output / "source" / "scb").glob("*.json"))
    target.write_text("{}\n")
    with pytest.raises(ValueError):
        collect(root, output, result["run_id"])
    assert not root.exists()


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
    assert result["counts"]["failed"] == 0
    assert result["counts"]["codelists_failed"] == 1
    assert len(parsed(root)) == 2
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
    from apps.harvest.metadata_adapters.pxweb_v2 import workflow
    from apps.harvest.storage import write

    source, root, output = Source(), tmp_path / "data", tmp_path / "second"
    harvest(source, root, tmp_path / "first")
    before = workflow.manifest(root)
    source.fail_languages.add("en")

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await workflow.run(PROVIDER, root, output, client=client)

    result = asyncio.run(execute())
    candidate = output
    tampered = SourceStore(candidate / "source", "pxweb_v2")
    for key in [k for k in tampered.entries() if k.endswith("/en")]:
        tampered.remove(key)
    tampered.save()
    for folder in ("codelists", "table-records"):
        next((candidate / folder).glob("*__en__*.jsonl*")).unlink()
    result["manifest"] = workflow.manifest(candidate)
    write(output / "result.json", result)
    with pytest.raises(ValueError, match="remov|scope|listing"):
        workflow.collect(root, output, result["run_id"])
    assert workflow.manifest(root) == before


def test_bad_listing_row_preserves_old_table_and_publishes_good_siblings(tmp_path):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    source.tables["sv"] = [{"id": "T1"}, table("later")]
    result = harvest(source, root, tmp_path / "second")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["removed"] == 0
    assert {
        d["extension"]["nordicintel"]["dataset_code"] for d in parsed(root).values()
    } == {"T1", "later", "EN1"}


def test_missing_manifest_entry_is_fetched(tmp_path):
    import shutil

    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    shutil.rmtree(root / "source")  # listings remain; the source entries are gone
    source.requests.clear()
    result = harvest(source, root, tmp_path / "second")
    assert result["counts"]["created"] == 2 and result["counts"]["reused"] == 0
    assert sum(path.endswith("/metadata") for path, _ in source.requests) == 2


def test_stale_artifact_is_rejected_and_budget_exhaustion_fails_remaining(tmp_path):
    from apps.harvest.metadata_adapters.pxweb_v2.workflow import collect

    source, root = Source(), tmp_path / "data"
    first = harvest(source, root, tmp_path / "first")
    source.requests.clear()
    result = harvest(source, root, tmp_path / "second", full=True, budget_seconds=0)
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["failed"] == 2
    assert not any(path.endswith("/metadata") for path, _ in source.requests)
    assert not (root / "work").exists()
    with pytest.raises(ValueError, match="Stale"):
        collect(root, tmp_path / "first", first["run_id"])
    third = harvest(source, root, tmp_path / "third")
    assert third["status"] == "success"
    assert third["counts"]["updated"] == 2


def test_confirmed_removed_codelist_is_not_requested_again_on_refresh(tmp_path):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first")
    original = source.handle

    def handler(request):
        if "/codelists/" in request.url.path and request.url.params["lang"] == "sv":
            source.requests.append((request.url.path, "sv"))
            return httpx.Response(404)
        return original(request)

    source.handle = handler
    result = harvest(source, root, tmp_path / "second", full=True)
    assert result["counts"]["codelists_failed"] == 1
    assert len(result["removed_codelists"]) == 1
    source.requests.clear()
    again = harvest(source, root, tmp_path / "third")
    assert again["counts"]["reused"] == 2
    assert again["counts"]["codelists_failed"] == 1
    assert "removed" in again["errors"][0]["error"]
    assert not any("/codelists/" in path for path, _ in source.requests)
    source.requests.clear()
    full = harvest(source, root, tmp_path / "fourth", full=True)
    assert ("/api/v2/codelists/vs_region", "sv") in source.requests
    assert full["counts"]["codelists_failed"] == 1

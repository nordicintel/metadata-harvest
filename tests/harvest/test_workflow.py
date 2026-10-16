import asyncio
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from apps.harvest.metadata_adapters.pxweb_v1.discovery import (
    _collect_database,
    get_pxweb_tables,
)
from apps.harvest.metadata_adapters.pxweb_v1.mapping import source_record
from apps.harvest.metadata_adapters.pxweb_v1.metadata import assemble_document
from apps.harvest.metadata_adapters.pxweb_v1.workflow import (
    build_provider,
    collect_metadata,
    collect_refresh,
    prepare,
    stored_key,
)
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import JsonlStore, filename, group_records, read, write
from apps.operations.harvest import publish
from tests.harvest.test_pxweb import website as website
from tests.support.parsing import documents

PROVIDER = {"code": "p", "languages": ["sv"], "database_ids": ["db"], "rate_limit": 0}
FETCH = "apps.harvest.metadata_adapters.pxweb_v1.workflow.get_pxweb_source"


def test_adapter_discovers_selected_provider_scopes(website):
    from apps.harvest.metadata_adapters.pxweb_v1.discovery import discover_provider

    base, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<a href="/pxweb/sv/db/-/good.px/">Good</a><time datetime="2026-01-01"></time>',
        {},
    )
    provider = {**PROVIDER, "base_web_url": base + "/pxweb/"}

    async def collect():
        return [scope async for scope in discover_provider(provider)]

    scopes = asyncio.run(collect())
    assert len(scopes) == 1
    assert scopes[0]["complete"] is True
    assert scopes[0]["language"] == "sv"
    assert scopes[0]["database_id"] == "db"
    assert [row["id"] for row in scopes[0]["records"]] == ["good.px"]


def record(code="t.px", path="/", date="2026-10-01", database="db"):
    return {
        "provider_code": "p",
        "dataset_code": code,
        "id": code,
        "language": "sv",
        "database_id": database,
        "path": path,
        "category_path": [],
        "published": date,
        "title": "Table",
        "api_url": f"https://example.test/api/v1/sv/{database}{path}/{code}",
        "web_url": "https://example.test/table",
    }


def scope(rows, *, complete=True, observed=None, invalid=None):
    return {
        "provider_code": "p",
        "run_id": "run",
        "phase": "refresh",
        "scopes": [
            {
                "language": "sv",
                "database_id": "db",
                "complete": complete,
                "records": rows,
                "observed_ids": observed
                if observed is not None
                else [row["id"] for row in rows],
                "invalid_ids": invalid or [],
                "errors": []
                if complete
                else [{"stage": "navigation", "message": "timeout"}],
            }
        ],
    }


def raw(row):
    """Source data as the worker stores it: GET metadata, no POST samples needed."""
    return {
        "responses": {
            "metadata": {
                "title": row["title"],
                "variables": [
                    {
                        "code": "Year",
                        "text": "Year",
                        "values": ["2026"],
                        "valueTexts": ["2026"],
                        "time": True,
                    }
                ],
            },
            "table": deepcopy(row),
        },
        "derived": {"samples": [], "queries": []},
    }


def dataset(row):
    return assemble_document(row, "p", raw(row))


def store(data):
    return SourceStore(Path(data) / "source", "pxweb_v1")


def parsed(data):
    """Parsed documents keyed by task filename."""
    found = documents("pxweb_v1", data, [PROVIDER])
    return {filename(d["extension"]["nordicintel"]): d for d in found.values()}


def seed(data, row, *, listing=True):
    grouped = group_records([row])[0]
    if listing:
        listings = JsonlStore(data / "table-records")
        listings.records[filename(grouped)] = grouped
        listings.save()
    source = store(data)
    source.put(
        stored_key(grouped),
        responses=raw(grouped)["responses"],
        derived=raw(grouped)["derived"],
        updated=grouped["published"],
        retrieved_at="2026-10-01T00:00:00+00:00",
    )
    source.save()
    return grouped


def test_missing_manifest_entry_is_fetched(tmp_path):
    row = group_records([record()])[0]
    listings = JsonlStore(tmp_path / "table-records")
    listings.records[filename(row)] = row
    listings.save()
    state = collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    assert state["counts"]["reused"] == 0
    assert [task["kind"] for task in state["tasks"]] == ["created"]
    assert state["layout"] == 2


def test_prepare_refuses_pre_layout_checkpoint(tmp_path):
    collect_refresh(tmp_path, [PROVIDER], [scope([record()])], "run")
    state = read(tmp_path / "work/current.json")
    del state["layout"]
    write(tmp_path / "work/current.json", state)
    with pytest.raises(ValueError, match="predates"):
        prepare(tmp_path, [PROVIDER])


def test_group_paths_one_identity_and_filesystem_safe_filename():
    a = record("Å/TEST:code.px", "/Forecast", "2026-10-01")
    b = record("Å/TEST:code.px", "/Archive", "2020-01-01")
    row = group_records([b, a, b])[0]
    assert len(row["paths"]) == 2 and row["api_url"] == a["api_url"]
    assert filename(row) == filename(b)
    assert not any(c in filename(row) for c in '/\\:<>"|?*')
    assert filename(record("A.px")).lower() != filename(record("a.px")).lower()


def test_refresh_queues_missing_and_changed_and_reuses_unchanged(tmp_path):
    old = seed(tmp_path, record("old"))
    changed = seed(tmp_path, record("changed"))
    changed["published"] = "2026-09-01"  # Provider correction backwards also rebuilds.
    state = collect_refresh(
        tmp_path, [PROVIDER], [scope([old, changed, record("new")])], "run"
    )
    assert state["counts"]["reused"] == 1
    assert {task["kind"] for task in state["tasks"]} == {"created", "updated"}
    assert parsed(tmp_path)[filename(changed)]["updated"] == "2026-10-01"
    report = read(tmp_path / "reports/refresh.json")
    assert set(report["records"][0]) == {
        "provider_code",
        "dataset_code",
        "language",
        "updated",
    }


def test_confirmed_absence_deletes_without_archive_and_return_requires_fetch(tmp_path):
    row = seed(tmp_path, record())
    state = collect_refresh(tmp_path, [PROVIDER], [scope([])], "run")
    assert state["counts"]["removed"] == 1
    assert not store(tmp_path).entries()
    assert not JsonlStore(tmp_path / "table-records").records
    assert not (tmp_path / "metadata").exists()
    state = collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    assert state["counts"]["reused"] == 0
    assert [task["kind"] for task in state["tasks"]] == ["created"]


def test_completed_metadata_without_listing_record_still_reconciles(tmp_path):
    row = seed(tmp_path, record())
    listings = JsonlStore(tmp_path / "table-records")
    del listings.records[filename(row)]
    listings.save()
    state = collect_refresh(tmp_path, [PROVIDER], [], "run")
    assert state["counts"]["removed"] == 0
    state = collect_refresh(tmp_path, [PROVIDER], [scope([])], "run")
    assert state["counts"]["removed"] == 1
    assert stored_key(row) not in store(tmp_path).entries()


@pytest.mark.parametrize(
    "result",
    [None, scope([], complete=False), scope([], invalid=["t.px"], observed=["t.px"])],
)
def test_missing_job_failed_scope_or_invalid_observed_table_cannot_confirm_removal(
    tmp_path, result
):
    row = seed(tmp_path, record())
    original = store(tmp_path).get(stored_key(row))
    state = collect_refresh(
        tmp_path, [PROVIDER], [] if result is None else [result], "run"
    )
    assert state["counts"]["removed"] == 0
    assert store(tmp_path).get(stored_key(row)) == original


def test_successful_row_from_incomplete_scope_is_saved(tmp_path):
    state = collect_refresh(
        tmp_path, [PROVIDER], [scope([record()], complete=False)], "run"
    )
    assert len(state["tasks"]) == 1
    assert read(tmp_path / "reports/refresh.json")["error_count"] == 1


def test_failed_canonical_scope_keeps_newer_primary_and_all_paths(tmp_path):
    old = seed(tmp_path, record(path="/new", date="2026-10-01"))
    state = collect_refresh(
        tmp_path,
        [PROVIDER],
        [scope([record(path="/archive", date="2020-01-01")], complete=False)],
        "run",
    )
    saved = JsonlStore(tmp_path / "table-records").records[filename(old)]
    assert len(saved["paths"]) == 2
    assert saved["published"] == old["published"]
    assert not state["tasks"]


def test_partial_scope_does_not_hide_successfully_refreshed_canonical_record(tmp_path):
    row = seed(tmp_path, record())
    state = collect_refresh(tmp_path, [PROVIDER], [scope([row], complete=False)], "run")
    assert state["counts"]["reused"] == 1


def test_provider_failures_are_isolated_and_old_metadata_survives(
    tmp_path, monkeypatch
):
    old = seed(tmp_path, record("bad"))
    updated = {**old, "published": "2026-10-02"}
    collect_refresh(tmp_path, [PROVIDER], [scope([updated, record("good")])], "run")

    async def fetch(row, **kwargs):
        if row["id"] == "bad":
            raise RuntimeError("provider failed")
        return raw(row)

    monkeypatch.setattr(FETCH, fetch)
    result = asyncio.run(
        build_provider(tmp_path, PROVIDER, "run", 1, tmp_path / "artifact")
    )
    assert [r["status"] for r in result["outcomes"]] == ["failed", "success"]
    final = collect_metadata(tmp_path, [tmp_path / "artifact/result.json"], "run", 1)
    assert final["status"] == "completed_with_errors"
    assert final["counts"]["created"] == 1 and final["counts"]["failed"] == 1
    assert parsed(tmp_path)[filename(old)]["updated"] == "2026-10-01"
    next_state = collect_refresh(
        tmp_path, [PROVIDER], [scope([updated, record("good")])], "run"
    )
    assert len(next_state["tasks"]) == 1
    assert next_state["counts"]["reused"] == 1


def test_deadline_checkpoints_remaining_work_and_resume_skips_refresh(
    tmp_path, monkeypatch
):
    collect_refresh(tmp_path, [PROVIDER], [scope([record()])], "run")
    result = asyncio.run(
        build_provider(
            tmp_path, PROVIDER, "run", 1, tmp_path / "artifact", budget_seconds=0
        )
    )
    assert result["outcomes"] == []
    final = collect_metadata(tmp_path, [tmp_path / "artifact/result.json"], "run", 1)
    assert final["continue"] and final["counts"]["unfinished"] == 1
    assert prepare(tmp_path, [PROVIDER]) == {
        "run_id": "run",
        "phase": "metadata",
        "full": False,
        "wave": 2,
        "providers": ["p"],
    }


def test_legacy_listing_file_and_source_artifact_collect_without_loss(tmp_path):
    row = group_records([record()])[0]
    # A legacy per-table listing file is still readable; the artifact is a store.
    write(tmp_path / "table-records" / filename(row), row)
    collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    artifact = tmp_path / "artifact"
    seed(artifact, row, listing=False)
    write(
        artifact / "result.json",
        {
            "provider_code": "p",
            "run_id": "run",
            "wave": 1,
            "phase": "metadata",
            "outcomes": [{"filename": filename(row), "status": "success"}],
        },
    )
    result = collect_metadata(tmp_path, [artifact / "result.json"], "run", 1)
    assert result["status"] == "success"
    assert parsed(tmp_path)[filename(row)] == dataset(row)
    assert store(tmp_path).get(stored_key(row))["retrieved_at"] == (
        "2026-10-01T00:00:00+00:00"
    )
    assert not list((tmp_path / "table-records").glob("*.json"))


def test_reuse_refreshes_listing_context_at_parse_without_refetching(tmp_path):
    row = seed(tmp_path, record())
    current = {**row, "title": "Corrected listing title"}
    result = collect_refresh(tmp_path, [PROVIDER], [scope([current])], "run")
    assert result["counts"]["reused"] == 1
    assert not result["tasks"]
    saved = parsed(tmp_path)[filename(row)]
    assert saved["label"] == row["title"]
    assert source_record(saved)["title"] == "Corrected listing title"
    assert saved["updated"] == row["published"]
    original = {p.name: p.read_bytes() for p in (tmp_path / "source").rglob("*.json")}
    collect_refresh(tmp_path, [PROVIDER], [scope([current])], "run")
    assert {
        p.name: p.read_bytes() for p in (tmp_path / "source").rglob("*.json")
    } == original


def test_other_path_with_different_variables_is_a_variant_not_a_path(tmp_path):
    canonical = record(path="/New", date="2026-10-01")
    archive = record(path="/Archive", date="2020-01-01")
    grouped = group_records([canonical, archive])[0]
    assert len(grouped["paths"]) == 2
    listings = JsonlStore(tmp_path / "table-records")
    listings.records[filename(grouped)] = grouped
    listings.save()
    source = raw(grouped)
    source["responses"]["variants"] = {
        "db/Archive": {"title": "Old", "variables": [{"code": "Region"}]}
    }
    source["derived"]["paths"] = [
        {
            "location": "db/Archive",
            "database_id": "db",
            "path": "/Archive",
            "api_url": archive["api_url"],
            "match": False,
        }
    ]
    assert source_record(assemble_document(grouped, "p", source))["paths"] == [
        {k: v for k, v in grouped["paths"][1].items()}
    ]
    assert (
        len(
            assemble_document(grouped, "p", source)["extension"]["nordicintel"].get(
                "paths", []
            )
        )
        <= 1
    )


def test_unserializable_dataset_does_not_poison_later_worker_results(
    tmp_path, monkeypatch
):
    collect_refresh(
        tmp_path, [PROVIDER], [scope([record("bad"), record("good")])], "run"
    )

    async def fetch(row, **kwargs):
        result = raw(row)
        if row["id"] == "bad":
            result["responses"]["metadata"]["invalid"] = float("nan")
        return result

    monkeypatch.setattr(FETCH, fetch)
    artifact = tmp_path / "artifact"
    result = asyncio.run(build_provider(tmp_path, PROVIDER, "run", 1, artifact))
    assert [outcome["status"] for outcome in result["outcomes"]] == [
        "failed",
        "success",
    ]
    assert list(SourceStore(artifact / "source", "pxweb_v1").entries()) == [
        stored_key(record("good"))
    ]


def test_no_progress_cannot_create_infinite_continuations(tmp_path):
    collect_refresh(tmp_path, [PROVIDER], [scope([record()])], "run")
    for wave in range(1, 4):
        final = collect_metadata(tmp_path, [], "run", wave)
    assert final["status"] == "interrupted" and not final["continue"]
    assert prepare(tmp_path, [PROVIDER])["phase"] == "metadata"


def test_no_work_still_saves_final_report(tmp_path):
    row = seed(tmp_path, record())
    collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    final = collect_metadata(tmp_path, [], "run", 1)
    assert final["status"] == "success"
    assert read(tmp_path / "reports/harvest.json")["counts"]["reused"] == 1


def test_stale_or_duplicate_results_rejected(tmp_path):
    with pytest.raises(ValueError, match="stale"):
        collect_refresh(tmp_path, [PROVIDER], [scope([record()])], "other")
    with pytest.raises(ValueError, match="Duplicate"):
        collect_refresh(tmp_path, [PROVIDER], [scope([]), scope([])], "run")
    collect_refresh(tmp_path, [PROVIDER], [scope([record()])], "run")
    with pytest.raises(ValueError, match="Stale"):
        collect_metadata(tmp_path, [], "run", 99)


def test_atomic_write_preserves_previous_file_on_serialization_failure(tmp_path):
    path = tmp_path / "record.json"
    write(path, {"good": 1})
    with pytest.raises(ValueError):
        write(path, {"bad": float("nan")})
    assert read(path) == {"good": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_partial_browser_extraction_preserves_good_rows_and_observed_bad_ids(
    website, monkeypatch
):
    base, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<ul><li><a href="/pxweb/sv/db/-/good.px/">Good</a><time datetime="2026-01-01"></time></li><li><a href="/pxweb/sv/db/-/bad.px/"></a><time datetime="2026-01-01"></time></li></ul>',
        {},
    )

    async def no_enrichment(*args):
        pass

    monkeypatch.setattr(
        "apps.harvest.metadata_adapters.pxweb_v1.discovery._enrich_missing",
        no_enrichment,
    )

    async def collect():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                return await _collect_database(
                    browser, base + "/pxweb/sv/db/", "sv", "db"
                )
            finally:
                await browser.close()

    result = asyncio.run(collect())
    assert [row["id"] for row in result["records"]] == ["good.px"]
    assert result["observed_ids"] == ["bad.px", "good.px"]
    assert result["invalid_ids"] == ["bad.px"]
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(base + "/pxweb/sv/db/"))


def test_visible_rows_survive_incomplete_expansion(website, monkeypatch):
    base, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<ul><li><a href="/pxweb/sv/db/-/good.px/">Good</a><time datetime="2026-01-01"></time></li><li role="tree"><button aria-expanded="false">Folder</button></li></ul>',
        {},
    )

    async def stalled(*args):
        raise RuntimeError("Incomplete expansion")

    monkeypatch.setattr(
        "apps.harvest.metadata_adapters.pxweb_v1.discovery._activate", stalled
    )

    async def collect():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                return await _collect_database(
                    browser, base + "/pxweb/sv/db/", "sv", "db"
                )
            finally:
                await browser.close()

    result = asyncio.run(collect())
    assert not result["complete"]
    assert result["observed_ids"] == ["good.px"]
    assert [row["id"] for row in result["records"]] == ["good.px"]
    assert "Incomplete expansion" in result["errors"][0]["message"]


def test_publish_rebases_unrelated_main_push_without_force(tmp_path, monkeypatch):
    def git(folder, *args):
        return subprocess.run(
            ["git", "-C", str(folder), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    remote, first, second = [tmp_path / name for name in ("remote", "first", "second")]
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "clone", str(remote), str(first)], check=True, capture_output=True
    )
    git(first, "checkout", "-b", "main")
    for folder in [first]:
        git(folder, "config", "user.name", "Test")
        git(folder, "config", "user.email", "test@example.invalid")
    (first / "README.md").write_text("initial")
    git(first, "add", ".")
    git(first, "commit", "-m", "initial")
    git(first, "push", "origin", "main")
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(second)],
        check=True,
        capture_output=True,
    )
    git(second, "config", "user.name", "Test")
    git(second, "config", "user.email", "test@example.invalid")
    (second / "README.md").write_text("unrelated update")
    git(second, "add", ".")
    git(second, "commit", "-m", "unrelated")
    git(second, "push", "origin", "main")
    write(first / "data/pxweb_v1/result.json", {"saved": True})
    monkeypatch.chdir(first)
    publish("harvest")
    assert (first / "README.md").read_text() == "unrelated update"
    assert read(first / "data/pxweb_v1/result.json") == {
        "saved": True,
        "profile_version": "2.0.0",
    }
    (first / "README.md").write_text("unrelated staged edit")
    git(first, "add", "README.md")
    with pytest.raises(RuntimeError, match="unrelated staged"):
        publish("must not commit")


def test_reports_replace_previous_run_and_completed_checkpoint_is_removed(tmp_path):
    row = seed(tmp_path, record())
    for run_id in ("first", "second"):
        result = {**scope([row]), "run_id": run_id}
        collect_refresh(tmp_path, [PROVIDER], [result], run_id)
        collect_metadata(tmp_path, [], run_id, 1)
    assert not (tmp_path / "work/current.json").exists()
    assert sorted(p.name for p in (tmp_path / "reports").rglob("*.json")) == [
        "harvest.json",
        "refresh.json",
    ]
    assert read(tmp_path / "reports/harvest.json")["run_id"] == "second"


def test_adapter_output_directories_are_independent(tmp_path):
    v1 = tmp_path / "pxweb_v1"
    other = tmp_path / "pxweb_v2"
    row = seed(v1, record())
    seed(other, record())
    original = {p.relative_to(other): p.read_bytes() for p in other.rglob("*.json*")}
    collect_refresh(v1, [PROVIDER], [scope([])], "run")
    assert stored_key(row) not in store(v1).entries()
    assert {
        p.relative_to(other): p.read_bytes() for p in other.rglob("*.json*")
    } == original


def test_full_refresh_queues_unchanged_metadata_and_preserves_mode(tmp_path):
    row = seed(tmp_path, record())
    state = collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run", full=True)
    assert state["full"] is True
    assert state["counts"]["reused"] == 0
    assert len(state["tasks"]) == 1
    assert prepare(tmp_path, [PROVIDER])["full"] is True


def test_failed_full_refresh_retries_unchanged_table_after_incomplete_discovery(
    tmp_path, monkeypatch
):
    row = seed(tmp_path, record())
    collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run", full=True)

    async def failed(*args, **kwargs):
        raise RuntimeError("temporary provider failure")

    monkeypatch.setattr(FETCH, failed)
    asyncio.run(build_provider(tmp_path, PROVIDER, "run", 1, tmp_path / "artifact"))
    collect_metadata(tmp_path, [tmp_path / "artifact/result.json"], "run", 1)
    # A later failed listing must not erase unfinished replacement failures.
    collect_refresh(tmp_path, [PROVIDER], [scope([], complete=False)], "run")
    deferred = collect_metadata(tmp_path, [], "run", 1)
    assert (
        deferred["status"] == "completed_with_errors"
        and deferred["counts"]["failed"] == 1
    )
    state = collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    assert state["counts"]["reused"] == 0
    assert [task["kind"] for task in state["tasks"]] == ["updated"]

    async def succeeded(row, **kwargs):
        return raw(row)

    monkeypatch.setattr(FETCH, succeeded)
    asyncio.run(build_provider(tmp_path, PROVIDER, "run", 1, tmp_path / "retry"))
    final = collect_metadata(tmp_path, [tmp_path / "retry/result.json"], "run", 1)
    assert final["status"] == "success" and final["counts"]["failed"] == 0
    assert read(tmp_path / "reports/harvest.json")["errors"] == []
    state = collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    assert state["counts"]["reused"] == 1 and not state["tasks"]

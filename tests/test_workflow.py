import asyncio
import subprocess
from copy import deepcopy

import pytest
from playwright.async_api import async_playwright

from metadata_harvest import get_pxweb_tables
from metadata_harvest.adapters.pxweb_v1.discovery import _collect_database
from metadata_harvest.runner import publish
from metadata_harvest.storage import JsonlStore, filename, group_records, read, write
from metadata_harvest.workflow import (
    build_provider,
    collect_metadata,
    collect_refresh,
    prepare,
)
from tests.test_pxweb import website as website

PROVIDER = {"code": "p", "languages": ["sv"], "database_ids": ["db"], "rate_limit": 0}


def test_adapter_discovers_selected_provider_scopes(website):
    from metadata_harvest.adapters.pxweb_v1.discovery import discover_provider

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


def dataset(row):
    return {
        "version": "2.0",
        "class": "dataset",
        "label": row["title"],
        "updated": row["published"],
        "id": ["Year"],
        "size": [1],
        "dimension": {
            "Year": {"category": {"index": ["2026"], "label": {"2026": "2026"}}}
        },
        "value": [],
        "extension": {"metadata_harvest": deepcopy(row)},
    }


def seed(data, row):
    grouped = group_records([row])[0]
    listings = JsonlStore(data / "table-records")
    listings.records[filename(grouped)] = grouped
    listings.save()
    metadata = JsonlStore(data / "metadata")
    metadata.records[filename(grouped)] = dataset(grouped)
    metadata.save()
    return grouped


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
    assert (
        JsonlStore(tmp_path / "metadata").records[filename(changed)]["updated"]
        == "2026-10-01"
    )
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
    assert not JsonlStore(tmp_path / "metadata").records
    assert not JsonlStore(tmp_path / "table-records").records
    assert not (tmp_path / "metadata/inactive").exists()
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
    assert filename(row) not in JsonlStore(tmp_path / "metadata").records


@pytest.mark.parametrize(
    "result",
    [None, scope([], complete=False), scope([], invalid=["t.px"], observed=["t.px"])],
)
def test_missing_job_failed_scope_or_invalid_observed_table_cannot_confirm_removal(
    tmp_path, result
):
    row = seed(tmp_path, record())
    original = JsonlStore(tmp_path / "metadata").records[filename(row)]
    state = collect_refresh(
        tmp_path, [PROVIDER], [] if result is None else [result], "run"
    )
    assert state["counts"]["removed"] == 0
    assert JsonlStore(tmp_path / "metadata").records[filename(row)] == original


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
        return dataset(row)

    monkeypatch.setattr("metadata_harvest.workflow.get_pxweb_metadata", fetch)
    result = asyncio.run(
        build_provider(tmp_path, PROVIDER, "run", 1, tmp_path / "artifact")
    )
    assert [r["status"] for r in result["outcomes"]] == ["failed", "success"]
    final = collect_metadata(tmp_path, [tmp_path / "artifact/result.json"], "run", 1)
    assert final["status"] == "completed_with_errors"
    assert final["counts"]["created"] == 1 and final["counts"]["failed"] == 1
    assert (
        JsonlStore(tmp_path / "metadata").records[filename(old)]["updated"]
        == "2026-10-01"
    )
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
        "wave": 2,
        "providers": ["p"],
    }


def test_legacy_checkpoint_and_artifact_migrate_without_losing_metadata(tmp_path):
    row = group_records([record()])[0]
    # A worker and work list created before the deployment are still readable.
    write(tmp_path / "table-records" / filename(row), row)
    collect_refresh(tmp_path, [PROVIDER], [scope([row])], "run")
    artifact = tmp_path / "artifact"
    write(artifact / "metadata" / filename(row), dataset(row))
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
    assert JsonlStore(tmp_path / "metadata").records[filename(row)] == dataset(row)
    assert len(list((tmp_path / "metadata").glob("*.jsonl"))) == 1
    assert not list((tmp_path / "metadata").glob("*.json"))
    assert not list((tmp_path / "table-records").glob("*.json"))


def test_legacy_reused_metadata_migrates_with_updated_listing_title(tmp_path):
    row = group_records([record()])[0]
    write(tmp_path / "table-records" / filename(row), row)
    write(tmp_path / "metadata" / filename(row), dataset(row))
    current = {**row, "title": "Corrected listing title"}
    result = collect_refresh(tmp_path, [PROVIDER], [scope([current])], "run")
    assert result["counts"]["reused"] == 1
    assert not result["tasks"]
    saved = JsonlStore(tmp_path / "metadata").records[filename(row)]
    assert saved["label"] == "Corrected listing title"
    assert saved["updated"] == row["published"]
    original = {p.name: p.read_bytes() for p in (tmp_path / "metadata").glob("*.jsonl")}
    collect_refresh(tmp_path, [PROVIDER], [scope([current])], "run")
    assert {
        p.name: p.read_bytes() for p in (tmp_path / "metadata").glob("*.jsonl")
    } == original


def test_unserializable_dataset_does_not_poison_later_worker_results(
    tmp_path, monkeypatch
):
    collect_refresh(
        tmp_path, [PROVIDER], [scope([record("bad"), record("good")])], "run"
    )

    async def fetch(row, **kwargs):
        result = dataset(row)
        if row["id"] == "bad":
            result["extension"]["invalid"] = float("nan")
        return result

    monkeypatch.setattr("metadata_harvest.workflow.get_pxweb_metadata", fetch)
    artifact = tmp_path / "artifact"
    result = asyncio.run(build_provider(tmp_path, PROVIDER, "run", 1, artifact))
    assert [outcome["status"] for outcome in result["outcomes"]] == [
        "failed",
        "success",
    ]
    assert list(JsonlStore(artifact / "metadata").records) == [filename(record("good"))]


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
        "metadata_harvest.adapters.pxweb_v1.discovery._enrich_missing", no_enrichment
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
        "metadata_harvest.adapters.pxweb_v1.discovery._activate", stalled
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
    write(first / "data/result.json", {"saved": True})
    monkeypatch.chdir(first)
    publish("harvest")
    assert (first / "README.md").read_text() == "unrelated update"
    assert read(first / "data/result.json") == {"saved": True}
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
    original = {p.relative_to(other): p.read_bytes() for p in other.rglob("*.jsonl")}
    collect_refresh(v1, [PROVIDER], [scope([])], "run")
    assert filename(row) not in JsonlStore(v1 / "metadata").records
    assert {
        p.relative_to(other): p.read_bytes() for p in other.rglob("*.jsonl")
    } == original

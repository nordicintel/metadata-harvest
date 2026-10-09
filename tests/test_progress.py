import json
from pathlib import Path

import pytest

from metadata_harvest.progress import Progress


@pytest.mark.parametrize("failures", [1, 2])
def test_progress_retries_transient_replace_without_exposing_partial_state(
    tmp_path, monkeypatch, failures
):
    path = tmp_path / "progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(path))
    progress = Progress("test", "provider")
    accepted = path.read_bytes()
    original_replace = Path.replace
    attempts = []
    delays = []

    def temporarily_locked(temporary, destination):
        attempts.append(temporary)
        if len(attempts) <= failures:
            assert path.read_bytes() == accepted
            assert json.loads(temporary.read_text())["processed"] == 7
            raise PermissionError("[WinError 5] Access is denied")
        return original_replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", temporarily_locked)
    monkeypatch.setattr("metadata_harvest.progress.time.sleep", delays.append)
    progress.update(processed=7)
    assert json.loads(path.read_text())["processed"] == 7
    assert len(attempts) == failures + 1
    assert len(delays) == failures and sum(delays) < 1


def test_progress_permanent_replace_failure_propagates_and_preserves_state(
    tmp_path, monkeypatch
):
    path = tmp_path / "progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(path))
    progress = Progress("test", "provider")
    accepted = path.read_bytes()
    attempts = []
    delays = []

    def permanently_locked(temporary, destination):
        attempts.append(temporary)
        raise PermissionError("permanently denied")

    monkeypatch.setattr(Path, "replace", permanently_locked)
    monkeypatch.setattr("metadata_harvest.progress.time.sleep", delays.append)
    with pytest.raises(PermissionError, match="permanently denied"):
        progress.update(processed=7)
    assert path.read_bytes() == accepted
    assert len(attempts) == 4
    assert len(delays) == 3 and sum(delays) < 1


def test_progress_separates_failures_and_keeps_every_error(tmp_path, monkeypatch):
    path = tmp_path / "progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(path))
    progress = Progress("pxweb_v2", "scb")
    progress.update(phase="metadata", total=3, processed=0, succeeded=0, failed=0)
    for number in range(55):
        progress.error("codelist", str(number), "sv", f"HTTP 503: {number}")
    progress.error("metadata", "table1", "en", "wrong categories")
    progress.update(processed=2, succeeded=1, failed=1, current="table3")
    state = json.loads(path.read_text())
    errors = [
        json.loads(line)
        for line in path.with_suffix(".errors.jsonl").read_text().splitlines()
    ]
    assert state["remaining"] == 1
    assert state["failed"] == 1
    assert state["errors_by_stage"] == {"codelist": 55, "metadata": 1}
    assert len(errors) == 56
    assert errors[-1]["resource"] == "table1"
    assert errors[-1]["language"] == "en"
    assert errors[-1]["error"] == "wrong categories"
    assert state["eta_seconds"] is None


def test_progress_does_not_write_inside_collections_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HARVEST_PROGRESS", raising=False)
    monkeypatch.chdir(tmp_path)
    Progress("pxweb_v1", "p").update(total=1)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "config,expected",
    [
        (dict(maxCallsPerTimeWindow=30, timeWindow=10), 1 / 3),
        (dict(maxCallsPerTimeWindow=2, timeWindow=10), 5),
    ],
)
def test_pacing_uses_provider_limit(config, expected):
    import asyncio

    from metadata_harvest.adapters.pxweb_v2.client import Client

    async def check():
        async with Client() as client:
            client.configure(config)
            assert client.interval == pytest.approx(expected)

    asyncio.run(check())

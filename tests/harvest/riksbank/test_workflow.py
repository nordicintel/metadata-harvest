"""Weekly Riksbank lifecycle against recorded responses."""

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.harvest.metadata_adapters.riksbank.client import Unreachable
from apps.harvest.metadata_adapters.riksbank.state import (
    collection,
    load_documents,
    load_state,
)
from tests.support.riksbank import API, RecordedClient, fixture_key

NOW = datetime(2026, 10, 12, 5, 15, tzinfo=UTC)
PROVIDER = {"code": "riksbank", "languages": ["en"], "retry_cooldown_seconds": 0}
GROUPS = fixture_key(f"{API}/swea/v1/Groups")
SERIES = fixture_key(f"{API}/swea/v1/Series")
MPD = f"{API}/monetary_policy_data/v1/forecasts"
SERIES_IDS = fixture_key(f"{MPD}/series_ids")
ROUNDS = fixture_key(f"{MPD}/policy_rounds")
SWESTR = fixture_key(f"{API}/swestr/v1/latest/SWESTR")
LATEST_2 = fixture_key(f"{API}/swea/v1/Observations/Latest/ByGroup/2")
ALL = {
    "en/swea_group_2",
    "en/swea_group_130",
    "en/swea_group_151",
    "en/swea_group_155",
    "en/mpd_SEQGDPNAYSA",
    "en/mpd_SEMCPIFNAYNA",
    "en/mpd_SEAWAGNAYCA",
    "en/swestr",
}


class Source(RecordedClient):
    """Recorded answers; failing or unrecorded requests are unreachable, like
    ``RiksbankClient`` after its retries."""

    async def get_json(self, url, params=None, *, missing_ok=False):
        try:
            return await super().get_json(url, params, missing_ok=missing_ok)
        except (TimeoutError, LookupError) as error:
            raise Unreachable(str(error)) from error


def start(source, data, output, *, clock=NOW, **options):
    from apps.harvest.metadata_adapters.riksbank.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from apps.harvest.metadata_adapters.riksbank.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["en"])
    return result


def published(data):
    return load_documents(data, ["en"])


@pytest.fixture(scope="module")
def bootstrapped(tmp_path_factory):
    """One bootstrap per module; each test gets a copy of its published data."""
    root = tmp_path_factory.mktemp("bootstrap")
    source = Source()
    result = execute(source, root / "data", root / "output")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(published(root / "data")) == 8
    assert result["requests"] == source.request_count == 12
    return root / "data"


@pytest.fixture
def data(bootstrapped, tmp_path):
    return Path(shutil.copytree(bootstrapped, tmp_path / "data"))


def test_bootstrap_publishes_valid_documents(data):
    from apps.harvest.validation import validate_metadata

    documents = published(data)
    assert set(documents) == ALL
    for document in documents.values():
        validate_metadata(document, adapter="riksbank")
        assert document["value"] == []
        assert document["extension"]["riksbank"]["api"] in {"swea", "mpd", "swestr"}


def test_rerun_is_byte_identical_and_unchanged(data, tmp_path):
    before = (data / collection("en")).read_bytes()
    result = execute(Source(), data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 8
    assert (data / collection("en")).read_bytes() == before


def test_failing_probe_is_withheld_then_restored(data, tmp_path):
    source = Source()
    source.failing = {LATEST_2}
    before = published(data)["en/swea_group_2"]
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("endpoint", "en/swea_group_2")
    ]
    assert source.requests.count(LATEST_2) == 2  # retried once after the cooldown
    assert result["counts"]["withheld"] == 1
    assert "en/swea_group_2" not in published(data)
    assert load_state(data)["withheld"]["en/swea_group_2"] == before

    result = execute(Source(), data, tmp_path / "back")
    assert result["counts"]["restored"] == 1
    assert published(data)["en/swea_group_2"] == before


def test_failing_listing_row_before_valid_rows(data, tmp_path):
    source = Source()
    source.responses[SERIES].insert(0, {"seriesId": "BROKEN ROW"})
    result = execute(source, data, tmp_path / "row")
    assert result["status"] == "completed_with_errors"
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "swea/series.0")
    ]
    assert result["apis"]["swea"] == "incomplete"
    assert result["counts"]["unchanged"] == 8 and result["counts"]["removed"] == 0
    assert set(published(data)) == ALL


def test_duplicates_are_quarantined_and_keep_their_documents(data, tmp_path):
    before = published(data)
    source = Source()
    duplicate = dict(source.responses[SERIES][0], groupId=130)  # SECBREPOEFF again
    source.responses[SERIES].append(duplicate)
    result = execute(source, data, tmp_path / "duplicates")
    assert {(e["stage"], e["resource"]) for e in result["errors"]} == {
        ("discovery", "en/swea_group_2"),
        ("discovery", "en/swea_group_130"),
    }
    assert result["counts"]["unchanged"] == 6 and result["counts"]["removed"] == 0
    assert published(data) == before


def test_one_failing_api_keeps_the_others(data, tmp_path):
    source = Source()
    source.failing = {GROUPS}
    result = execute(source, data, tmp_path / "swea-down")
    assert result["status"] == "completed_with_errors"
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "swea")
    ]
    assert result["apis"] == {"swea": "failed", "mpd": "complete", "swestr": "complete"}
    assert result["counts"]["unchanged"] == 4 and result["counts"]["withheld"] == 4
    assert set(published(data)) == {k for k in ALL if "swea_" not in k}

    from apps.operations.outcome import _discovery

    assert _discovery(result, resource=lambda r: r in {"swea", "mpd", "swestr"}) == [
        "swea"
    ]


def test_swestr_failure_is_its_discovery_scope(data, tmp_path):
    source = Source()
    source.failing = {SWESTR}
    result = execute(source, data, tmp_path / "swestr-down")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "swestr")
    ]
    assert "en/swestr" in load_state(data)["withheld"]
    assert result["counts"]["unchanged"] == 7


def test_malformed_catalogue_keeps_documents_published(data, tmp_path):
    source = Source()
    source.responses[ROUNDS] = {"data": "not a list"}
    result = execute(source, data, tmp_path / "mpd-broken")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "mpd")
    ]
    assert set(published(data)) == ALL  # parsing failed; the endpoint answered

    later = NOW + timedelta(days=61)
    result = execute(source, data, tmp_path / "stale", clock=later)
    assert not any("mpd_" in key for key in published(data))
    assert result["counts"]["withheld"] == 3


def test_metadata_failure_expires_after_max_age(data, tmp_path):
    source = Source()
    source.missing = {
        fixture_key(MPD, {"series": "SEAWAGNAYCA", "policy_round_name": "2026:3"})
    }
    result = execute(source, data, tmp_path / "no-vintage")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("metadata", "en/mpd_SEAWAGNAYCA")
    ]
    assert "en/mpd_SEAWAGNAYCA" in published(data)

    execute(source, data, tmp_path / "expired", clock=NOW + timedelta(days=61))
    assert "en/mpd_SEAWAGNAYCA" not in published(data)
    assert "en/mpd_SEAWAGNAYCA" in load_state(data)["withheld"]


def test_every_api_failing_raises_and_publishes_nothing(data, tmp_path):
    source = Source()
    source.failing = {GROUPS, SERIES_IDS, SWESTR}
    before = (data / collection("en")).read_bytes()
    with pytest.raises(Unreachable):
        start(source, data, tmp_path / "out")
    assert not (tmp_path / "out" / "result.json").exists()
    assert (data / collection("en")).read_bytes() == before


def test_group_missing_from_listing_is_removed_only_by_unbounded_run(data, tmp_path):
    source = Source()
    source.responses[SERIES] = [
        row for row in source.responses[SERIES] if row["groupId"] != 155
    ]
    result = execute(source, data, tmp_path / "bounded", subjects=["swea_group_155"])
    assert "en/swea_group_155" in published(data)
    assert result["counts"]["removed"] == 0

    result = execute(source, data, tmp_path / "unbounded")
    assert result["counts"]["removed"] == 1
    assert "en/swea_group_155" not in published(data)
    assert "en/swea_group_155" not in load_state(data)["datasets"]


def test_bounded_run_touches_only_selected(data, tmp_path):
    later = NOW + timedelta(days=1)
    source = Source()
    result = execute(
        source,
        data,
        tmp_path / "bounded",
        clock=later,
        subjects=["swea_group_2", "SEQGDPNAYSA", "151"],
    )
    assert result["status"] == "success"
    assert result["counts"]["unchanged"] == 3
    touched = {
        key
        for key, info in load_state(data)["datasets"].items()
        if info["checked_at"] == later.isoformat()
    }
    assert touched == {"en/swea_group_2", "en/mpd_SEQGDPNAYSA", "en/swea_group_151"}
    assert SWESTR not in source.requests  # SWESTR was not selected


def test_unknown_subject_is_reported(data, tmp_path):
    result = execute(Source(), data, tmp_path / "unknown", subjects=["NOSUCH"])
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "NOSUCH")
    ]
    assert "Unknown" in result["errors"][0]["error"]
    assert result["counts"]["unchanged"] == 0


@pytest.mark.parametrize(
    ("subject", "failed_catalogue", "key"),
    [
        ("SECBREPOEFF", GROUPS, "en/swea_group_2"),
        ("SEQGDPNAYSA", SERIES_IDS, "en/mpd_SEQGDPNAYSA"),
    ],
)
def test_discovery_failure_resolves_previously_accepted_series_alias(
    data, tmp_path, subject, failed_catalogue, key
):
    source = Source()
    source.failing = {failed_catalogue}
    before = published(data)[key]
    result = execute(
        source, data, tmp_path / "alias-down", subjects=[subject, "swestr"]
    )
    assert result["status"] == "completed_with_errors"
    assert key not in published(data)
    assert load_state(data)["withheld"][key] == before
    assert result["counts"]["withheld"] == 1


@pytest.mark.parametrize("stage", ["discovery", "endpoint"])
def test_live_failure_has_url_before_next_sibling(data, tmp_path, monkeypatch, stage):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(progress_path))
    failed = GROUPS if stage == "discovery" else LATEST_2
    sibling = (
        SERIES_IDS
        if stage == "discovery"
        else fixture_key(f"{API}/swea/v1/Observations/Latest/ByGroup/130")
    )

    class ObservedSource(Source):
        async def get_json(self, url, params=None, **kwargs):
            if fixture_key(url, params) == sibling:
                entries = [
                    json.loads(line)
                    for line in progress_path.with_suffix(".errors.jsonl")
                    .read_text()
                    .splitlines()
                ]
                assert any(
                    e["stage"] == stage and e["url"] == failed[4:] for e in entries
                )
            return await super().get_json(url, params, **kwargs)

    source = ObservedSource()
    source.failing = {failed}
    result = execute(source, data, tmp_path / "live-errors")
    assert result["status"] == "completed_with_errors"
    assert len(result["errors"]) == 1


@pytest.mark.parametrize("failed", [GROUPS, LATEST_2])
def test_recovered_failure_stays_in_telemetry_but_not_final_report(
    data, tmp_path, monkeypatch, failed
):
    progress_path = tmp_path / "progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(progress_path))

    class RecoveringSource(Source):
        async def get_json(self, url, params=None, **kwargs):
            if fixture_key(url, params) in self.failing:
                self.failing.remove(fixture_key(url, params))
                raise Unreachable("temporary failure")
            return await super().get_json(url, params, **kwargs)

    source = RecoveringSource()
    source.failing = {failed}
    result = execute(source, data, tmp_path / "recovered")
    assert result["status"] == "success"
    assert result["errors"] == []
    assert result["counts"]["failed"] == result["counts"]["withheld"] == 0
    entries = [
        json.loads(line)
        for line in progress_path.with_suffix(".errors.jsonl").read_text().splitlines()
    ]
    assert len(entries) == 1 and entries[0]["url"] == failed[4:]


def test_stale_artifact_rejected(data, tmp_path):
    from apps.harvest.metadata_adapters.riksbank.workflow import collect

    start(Source(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["en"])


def test_unconfigured_language_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="languages"):
        start(Source(), tmp_path / "data", tmp_path / "out", languages=["sv"])

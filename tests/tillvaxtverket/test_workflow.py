"""Weekly Tillväxtverket lifecycle against recorded responses."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from metadata_harvest.adapters.tillvaxtverket import source
from metadata_harvest.adapters.tillvaxtverket.client import Unreachable
from metadata_harvest.adapters.tillvaxtverket.state import (
    collection,
    load_documents,
    load_state,
)
from tests.tillvaxtverket.conftest import (
    RecordedClient,
    dcat_key,
    distinct_key,
    labels_key,
    probe_key,
)

NOW = datetime(2026, 10, 12, 5, 45, tzinfo=UTC)
PROVIDER = {"code": "tillvaxtverket", "languages": ["sv"], "retry_cooldown_seconds": 0}
NAMES = [spec.name for spec in source.DATASETS]
YEAR = "GuestNights_Country_Year"
REVENUE = "GuestNights_Capacity_Revenue_Year"


class Source(RecordedClient):
    """Recorded answers; failing or unrecorded requests are unreachable, like
    ``TillvaxtverketClient`` after its retries."""

    def _answer(self, url, params):
        try:
            return super()._answer(url, params)
        except (TimeoutError, LookupError) as error:
            raise Unreachable(str(error)) from error


def start(client, data, output, *, clock=NOW, **options):
    from metadata_harvest.adapters.tillvaxtverket.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=client, clock=clock, **options)
    )


def execute(client, data, output, **options):
    from metadata_harvest.adapters.tillvaxtverket.workflow import collect

    result = start(client, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data):
    return load_documents(data, ["sv"])


def key(name):
    return f"sv/inkvartering/{name}"


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    result = execute(Source(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 6 == len(published(root))
    return root


def test_bootstrap_publishes_six_valid_documents(data):
    from metadata_harvest.validation import validate_metadata

    documents = published(data)
    assert sorted(documents) == sorted(key(name) for name in NAMES)
    for document in documents.values():
        validate_metadata(document, adapter="tillvaxtverket")
        assert document["extension"]["nordicintel"]["official_statistics"] is True
        assert document["extension"]["tillvaxtverket"]["source_record"]["load_dates"]
    state = load_state(data)["datasets"]
    assert {info["status"] for info in state.values()} == {"published"}


def test_rerun_is_byte_identical_and_reuses_cubes(data, tmp_path):
    before = (data / collection("sv")).read_bytes()
    client = Source()
    result = execute(client, data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 6
    assert (data / collection("sv")).read_bytes() == before
    # Only the change signal and data.europa.eu: no distinct-value queries.
    assert client.request_count == 6 * 3
    assert not any("dimension=NIVA_NAMN" in r for r in client.requests)


def test_new_load_rebuilds_only_that_dataset(data, tmp_path):
    client = Source()
    loads = client.responses[distinct_key(YEAR, "LOAD_DATE")]
    loads["rows"].append({"LOAD_DATE": "2026-10-11, 20:00:00"})
    loads["rowCount"] += 1
    result = execute(client, data, tmp_path / "reload")
    assert result["counts"]["rebuilt"] == 1
    assert result["counts"]["unchanged"] == 5
    document = published(data)[key(YEAR)]
    assert document["updated"] == "2026-10-11"
    assert (
        document["extension"]["tillvaxtverket"]["updated_original"]
        == "2026-10-11, 20:00:00"
    )


def test_failing_endpoint_is_withheld_retried_then_restored(data, tmp_path):
    before = published(data)[key(YEAR)]
    client = Source()
    client.failing = {probe_key(YEAR)}
    result = execute(client, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("endpoint", key(YEAR))
    ]
    assert result["errors"][0]["url"].endswith(f"{YEAR}.cbase")
    assert client.requests.count(probe_key(YEAR)) == 2  # retried once
    assert result["counts"]["withheld"] == 1
    assert key(YEAR) not in published(data)
    assert load_state(data)["withheld"][key(YEAR)] == before

    result = execute(Source(), data, tmp_path / "back")
    assert result["counts"]["restored"] == 1
    assert published(data)[key(YEAR)] == before


def test_failing_dataset_before_valid_siblings(data, tmp_path):
    """The first registered dataset fails to parse; the others still publish."""
    first = NAMES[0]
    before = published(data)[key(first)]
    client = Source()
    client.responses[probe_key(first)]["columns"].append(
        {"name": "NY_KOLUMN", "type": "integer"}
    )
    result = execute(client, data, tmp_path / "bad-first")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("metadata", key(first))
    ]
    assert "NY_KOLUMN" in result["errors"][0]["error"]
    assert result["counts"]["unchanged"] == 5
    assert published(data)[key(first)] == before  # endpoint works: still published


def test_new_dataset_failure_has_no_document(tmp_path):
    client = Source()
    client.responses[probe_key(NAMES[0])]["columns"].pop()
    result = execute(client, tmp_path / "data", tmp_path / "out")
    assert result["counts"]["rebuilt"] == 5
    assert key(NAMES[0]) not in published(tmp_path / "data")
    assert load_state(tmp_path / "data")["datasets"][key(NAMES[0])]["status"] == (
        "missing"
    )


def test_dcat_failure_keeps_label_and_is_a_dependency_error(data, tmp_path):
    before = published(data)
    client = Source()
    client.failing = {dcat_key(YEAR)}
    result = execute(client, data, tmp_path / "dcat-down")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("dependency", f"{key(YEAR)}#data.europa.eu")
    ]
    assert result["counts"]["dependency_failed"] == 1
    assert result["counts"]["failed"] == 0
    assert published(data) == before


def test_dcat_failure_on_bootstrap_falls_back_then_relabels(tmp_path):
    root = tmp_path / "data"
    client = Source()
    client.failing = {dcat_key(YEAR)}
    execute(client, root, tmp_path / "first")
    document = published(root)[key(YEAR)]
    assert document["label"] == YEAR
    assert "description" not in document["extension"]["nordicintel"]

    result = execute(Source(), root, tmp_path / "second")
    assert result["counts"]["relabelled"] == 1
    assert published(root)[key(YEAR)]["label"] == "Gästnätter per land och år"


def test_label_file_failure_falls_back_and_retries(tmp_path):
    root = tmp_path / "data"
    client = Source()
    client.failing = {labels_key(REVENUE)}
    result = execute(client, root, tmp_path / "first")
    assert ("dependency", f"{key(REVENUE)}#labels") in [
        (e["stage"], e["resource"]) for e in result["errors"]
    ]
    measures = published(root)[key(REVENUE)]["dimension"]["ContentsCode"]
    assert measures["category"]["label"]["LOGIINTAKT"] == "LOGIINTAKT"

    client = Source()
    result = execute(client, root, tmp_path / "second")
    assert result["counts"]["rebuilt"] == 1  # forced although the signal is unchanged
    measures = published(root)[key(REVENUE)]["dimension"]["ContentsCode"]
    assert measures["category"]["label"]["LOGIINTAKT"] == "Logiintäkt"


def test_conflicting_duplicates_are_quarantined(data, tmp_path):
    before = published(data)[key(YEAR)]
    client = Source()
    client.responses[distinct_key(YEAR, "LOAD_DATE")]["rows"].append(
        {"LOAD_DATE": "2026-10-11, 20:00:00"}
    )
    client.responses[distinct_key(YEAR, "LOAD_DATE")]["rowCount"] += 1
    countries = client.responses[
        distinct_key(
            YEAR,
            "LAND_KOD",
            "LAND_NAMN",
            "LANDGRP_SVUTL_NAMN",
            "LANDGRP_GROV_NAMN",
            "LANDGRP_20_NAMN",
            "LANDGRP_40_NAMN",
        )
    ]
    countries["rows"].append({**countries["rows"][0], "LAND_NAMN": "Annat namn"})
    countries["rowCount"] += 1
    result = execute(client, data, tmp_path / "duplicate")
    assert result["counts"]["quarantined"] == 1
    assert result["quarantined"] == [key(YEAR)]
    assert published(data)[key(YEAR)] == before


def test_metadata_failure_expires_after_max_age(data, tmp_path):
    client = Source()
    client.responses[probe_key(YEAR)]["columns"].pop()
    execute(client, data, tmp_path / "broken")
    assert key(YEAR) in published(data)
    later = NOW + timedelta(days=61)
    result = execute(client, data, tmp_path / "stale", clock=later)
    assert key(YEAR) not in published(data)
    assert load_state(data)["datasets"][key(YEAR)]["withheld_reason"] == "stale"
    assert result["counts"]["withheld"] == 1


def test_all_endpoints_failing_withholds_without_removing(data, tmp_path):
    client = Source()
    client.failing = {probe_key(name) for name in NAMES}
    result = execute(client, data, tmp_path / "outage")
    assert result["status"] == "completed_with_errors"
    assert {e["stage"] for e in result["errors"]} == {"endpoint"}
    assert result["counts"]["removed"] == 0
    assert len(load_state(data)["withheld"]) == 6
    assert published(data) == {}


def test_unexpected_published_file_raises_and_publishes_nothing(data, tmp_path):
    before = (data / collection("sv")).read_bytes()
    (data / "metadata" / "other.jsonl.gz").write_bytes(b"")
    with pytest.raises(ValueError, match="Unexpected file"):
        start(Source(), data, tmp_path / "out")
    assert (data / collection("sv")).read_bytes() == before
    assert not (tmp_path / "out").exists()


def test_dataset_dropped_from_registry_is_removed_only_in_unbounded_run(
    data, tmp_path, monkeypatch
):
    from metadata_harvest.adapters.tillvaxtverket import workflow

    kept = tuple(spec for spec in source.DATASETS if spec.name != YEAR)
    monkeypatch.setattr(workflow, "DATASETS", kept)
    result = execute(Source(), data, tmp_path / "bounded", products=[REVENUE])
    assert result["counts"]["removed"] == 0
    assert key(YEAR) in published(data)

    result = execute(Source(), data, tmp_path / "unbounded")
    assert result["counts"]["removed"] == 1
    assert key(YEAR) not in published(data)
    assert key(YEAR) not in load_state(data)["datasets"]


def test_bounded_run_touches_only_selected(data, tmp_path):
    later = NOW + timedelta(days=1)
    client = Source()
    result = execute(
        client,
        data,
        tmp_path / "bounded",
        clock=later,
        products=["guestnights_country_year", "inkvartering/" + REVENUE, "Nope"],
    )
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "Nope")
    ]
    assert "Unknown" in result["errors"][0]["error"]
    state = load_state(data)["datasets"]
    touched = {
        k for k, info in state.items() if info["checked_at"] == later.isoformat()
    }
    assert touched == {key(YEAR), key(REVENUE)}
    assert all(
        YEAR.casefold() in r.casefold() or REVENUE.casefold() in r.casefold()
        for r in client.requests
    )


def test_stale_artifact_rejected(data, tmp_path):
    from metadata_harvest.adapters.tillvaxtverket.workflow import collect

    start(Source(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])


def test_english_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Swedish only"):
        start(Source(), tmp_path / "data", tmp_path / "out", languages=["en"])

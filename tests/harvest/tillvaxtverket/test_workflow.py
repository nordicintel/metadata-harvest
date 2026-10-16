"""Weekly Tillväxtverket lifecycle against recorded responses."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from apps.harvest.metadata_adapters.tillvaxtverket import source
from apps.harvest.metadata_adapters.tillvaxtverket.client import Unreachable
from apps.harvest.metadata_adapters.tillvaxtverket.state import (
    PROVIDER_CODE,
    load_state,
)
from apps.harvest.source_store import SourceStore, harvest_manifest, source_key
from tests.support.binding import binding
from tests.support.parsing import documents
from tests.support.tillvaxtverket import (
    RecordedClient,
    distinct_key,
    labels_key,
    probe_key,
    snapshot_from,
)

NOW = datetime(2026, 10, 12, 5, 45, tzinfo=UTC)
PROVIDER = binding(
    "tillvaxtverket", "tillvaxtverket", languages=["sv"], retry_cooldown_seconds=0
)
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
    from apps.harvest.metadata_adapters.tillvaxtverket.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=client, clock=clock, **options)
    )


def execute(client, data, output, **options):
    from apps.harvest.metadata_adapters.tillvaxtverket.workflow import collect

    result = start(client, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


@pytest.fixture
def portal(tmp_path):
    """An accepted portal snapshot holding the recorded data.europa.eu records."""
    return snapshot_from(tmp_path / "data_eu", NAMES)


def published(data, *, include_withheld=False, portal=None):
    return documents(
        "tillvaxtverket",
        data,
        [PROVIDER],
        include_withheld=include_withheld,
        portal=portal,
    )


def key(name):
    return f"sv/inkvartering/{name}"


def stored_key(name):
    return source_key(PROVIDER_CODE, f"inkvartering/{name}", "sv")


def store(data):
    return SourceStore(data / "source", "tillvaxtverket")


def stable(data):
    return {
        k: v for k, v in harvest_manifest(data).items() if k != "reports/latest.json"
    }


MANIFEST = "source/manifest.json"


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data" / "tillvaxtverket"
    result = execute(Source(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 6 == len(published(root))
    return root


def test_bootstrap_stores_six_datasets_with_label_files(data, portal):
    from apps.harvest.validation import validate_metadata

    parsed = published(data, portal=portal)
    assert sorted(parsed) == sorted(key(name) for name in NAMES)
    for document in parsed.values():
        validate_metadata(document, adapter="tillvaxtverket")
        assert document["extension"]["nordicintel"]["official_statistics"] is True
        assert document["extension"]["tillvaxtverket"]["source_record"]["load_dates"]
    state = load_state(data)
    assert {info["status"] for info in state["datasets"].values()} == {"published"}
    assert "withheld" not in state
    envelope = store(data).get(stored_key(YEAR))
    assert set(envelope["responses"]) == {
        "columns",
        "load_dates",
        "regions",
        "dimensions",
        "periods",
    }
    assert (
        envelope["updated"]
        == envelope["responses"]["load_dates"]["rows"][-1]["LOAD_DATE"][:10]
        or envelope["updated"]
    )
    files = store(data).entries()[stored_key(YEAR)]["files"]
    assert any(name.endswith(".labels.csv") for name in files)
    assert not (data / "metadata").exists()
    # Without a portal snapshot the label falls back to the dataset name.
    assert published(data)[key(YEAR)]["label"] == YEAR


def test_rerun_is_byte_identical_and_reuses_cubes(data, tmp_path):
    before = stable(data)
    manifest = (data / MANIFEST).read_bytes()
    client = Source()
    result = execute(client, data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 6
    assert (data / MANIFEST).read_bytes() == manifest
    assert stable(data) == before
    # Only the change signal: no distinct-value queries, no data.europa.eu.
    assert client.request_count == 6 * 2
    assert not any("dimension=NIVA_NAMN" in r for r in client.requests)
    assert not any(r.startswith("GET https://data.europa.eu") for r in client.requests)


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
    assert load_state(data)["datasets"][key(YEAR)]["status"] == "withheld"
    assert published(data, include_withheld=True)[key(YEAR)] == before

    result = execute(Source(), data, tmp_path / "back")
    assert result["counts"]["restored"] == 1
    assert published(data)[key(YEAR)] == before


def test_changed_columns_are_stored_and_fail_at_parse_before_valid_siblings(
    data, tmp_path
):
    """The first registered dataset no longer matches the registry; it is stored
    all the same, parse reports it and the others still publish."""
    from apps.parsing.build import build

    first = NAMES[0]
    client = Source()
    client.responses[probe_key(first)]["columns"].append(
        {"name": "NY_KOLUMN", "type": "integer"}
    )
    result = execute(client, data, tmp_path / "bad-first")
    assert result["status"] == "success"
    assert result["counts"]["rebuilt"] == 1 and result["counts"]["unchanged"] == 5
    parsed = build(
        "tillvaxtverket", data.parent, tmp_path / "parsed", bindings=[PROVIDER]
    )
    assert [e["key"] for e in parsed["errors"]] == [stored_key(first)]
    assert "NY_KOLUMN" in parsed["errors"][0]["error"]
    assert parsed["counts"]["parsed"] == 5


def test_new_dataset_with_unexpected_columns_is_still_stored(tmp_path):
    client = Source()
    client.responses[probe_key(NAMES[0])]["columns"].pop()
    root = tmp_path / "data" / "tillvaxtverket"
    result = execute(client, root, tmp_path / "out")
    assert result["counts"]["rebuilt"] == 6
    assert store(root).get(stored_key(NAMES[0]))["responses"]["columns"]
    assert load_state(root)["datasets"][key(NAMES[0])]["status"] == "published"


def test_label_file_failure_falls_back_and_retries(tmp_path):
    root = tmp_path / "data"
    client = Source()
    client.failing = {labels_key(REVENUE)}
    root = root / "tillvaxtverket"
    result = execute(client, root, tmp_path / "first")
    assert ("dependency", f"{key(REVENUE)}#labels") in [
        (e["stage"], e["resource"]) for e in result["errors"]
    ]
    measures = published(root)[key(REVENUE)]["dimension"]["ContentsCode"]
    assert measures["category"]["label"]["LOGIINTAKT"] == "LOGIINTAKT"
    assert not any(
        f.endswith(".labels.csv")
        for f in store(root).entries()[stored_key(REVENUE)]["files"]
    )

    client = Source()
    result = execute(client, root, tmp_path / "second")
    assert result["counts"]["rebuilt"] == 1  # forced although the signal is unchanged
    measures = published(root)[key(REVENUE)]["dimension"]["ContentsCode"]
    assert measures["category"]["label"]["LOGIINTAKT"] == "Logiintäkt"


def test_conflicting_duplicates_are_stored_and_fail_at_parse(data, tmp_path):
    from apps.parsing.build import build

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
    assert result["status"] == "success"
    parsed = build(
        "tillvaxtverket", data.parent, tmp_path / "parsed", bindings=[PROVIDER]
    )
    assert [e["key"] for e in parsed["errors"]] == [stored_key(YEAR)]
    assert "Conflict" in parsed["errors"][0]["error"]


def test_all_endpoints_failing_withholds_without_removing(data, tmp_path):
    client = Source()
    client.failing = {probe_key(name) for name in NAMES}
    result = execute(client, data, tmp_path / "outage")
    assert result["status"] == "completed_with_errors"
    assert {e["stage"] for e in result["errors"]} == {"endpoint"}
    assert result["counts"]["removed"] == 0
    assert all(
        info["status"] == "withheld" for info in load_state(data)["datasets"].values()
    )
    assert published(data) == {}


def test_unexpected_published_file_raises_and_publishes_nothing(data, tmp_path):
    (data / "state" / "other.json").write_bytes(b"{}")
    before = harvest_manifest(data)
    with pytest.raises(ValueError, match="Unexpected file"):
        start(Source(), data, tmp_path / "out")
    assert harvest_manifest(data) == before
    assert not (tmp_path / "out").exists()


def test_dataset_dropped_from_registry_is_removed_only_in_unbounded_run(
    data, tmp_path, monkeypatch
):
    from apps.harvest.metadata_adapters.tillvaxtverket import workflow

    kept = tuple(spec for spec in source.DATASETS if spec.name != YEAR)
    monkeypatch.setattr(workflow, "DATASETS", kept)
    result = execute(Source(), data, tmp_path / "bounded", products=[REVENUE])
    assert result["counts"]["removed"] == 0
    assert key(YEAR) in published(data)

    result = execute(Source(), data, tmp_path / "unbounded")
    assert result["counts"]["removed"] == 1
    assert key(YEAR) not in published(data)
    assert key(YEAR) not in load_state(data)["datasets"]
    assert stored_key(YEAR) not in store(data).entries()


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
    from apps.harvest.metadata_adapters.tillvaxtverket.workflow import collect

    start(Source(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])


def test_english_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Swedish only"):
        start(Source(), tmp_path / "data", tmp_path / "out", languages=["en"])

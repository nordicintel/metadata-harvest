"""Weekly analysverktyg lifecycle against recorded answers."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from apps.harvest.metadata_adapters.dataanalys.client import Unreachable
from apps.harvest.metadata_adapters.dataanalys.state import (
    PROVIDER_CODE,
    load_state,
)
from apps.harvest.source_store import SourceStore, harvest_manifest, source_key
from tests.support.dataanalys import PROVIDER, RecordedClient, apply_synthetic
from tests.support.parsing import documents

NOW = datetime(2026, 10, 12, 6, 45, tzinfo=UTC)
BOOTSTRAP = {
    "sv/GONV|G013",
    "sv/GONV|G033",
    "sv/GONV|G082",
    "sv/GONV|G200",
    "sv/LRHST|LRHST09",
    "sv/NRAA|NRAA_1",
    "sv/NRAA|NRAA_2",
}


def start(source, data, output, *, clock=NOW, **options):
    from apps.harvest.metadata_adapters.dataanalys.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from apps.harvest.metadata_adapters.dataanalys.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data, *, include_withheld=False):
    return documents("dataanalys", data, [PROVIDER], include_withheld=include_withheld)


def stored_key(key):
    language, code = key.split("/", 1)
    return source_key(PROVIDER_CODE, code, language)


def store(data):
    return SourceStore(data / "source", "dataanalys")


def stable(data):
    return {
        k: v for k, v in harvest_manifest(data).items() if k != "reports/latest.json"
    }


MANIFEST = "source/manifest.json"


def resources(result):
    return [(e["stage"], e["resource"]) for e in result["errors"]]


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data" / "dataanalys"
    result = execute(RecordedClient(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(published(root)) == 7
    return root


def test_bootstrap_stores_listings_catalogues_and_cubes(data, tmp_path):
    from apps.harvest.storage import read
    from apps.harvest.validation import validate_metadata

    parsed = published(data)
    assert set(parsed) == BOOTSTRAP
    for document in parsed.values():
        validate_metadata(document, adapter="dataanalys")
        assert document["extension"]["socialstyrelsen_analys"]["source_record"]
    shared = set(store(data).manifest["shared"])
    assert {
        f"{PROVIDER_CODE}/_shared/{name}"
        for name in (
            "charts.json",
            "dashboards.json",
            "catalogue/GONV.json",
            "columns/GONV.json",
        )
    } <= shared
    envelope = store(data).get(stored_key("sv/GONV|G082"))
    assert set(envelope["responses"]) == {"cube"}
    assert set(envelope["derived"]) == {
        "row",
        "columns",
        "area",
        "link",
        "subject_label",
    }
    assert envelope["derived"]["row"]["MBusinessKey"] == "G082"
    assert envelope["updated"] == envelope["derived"]["row"]["DataGenerationDate"]
    assert "withheld" not in load_state(data)
    assert not (data / "metadata").exists()
    report = read(data / "reports/latest.json")
    assert report["areas"] == ["GONV", "LRHST", "NRAA"]
    assert report["unsupported_datasources"] == [
        "MART.vai_cancer_forekomst_femar_total"
    ]
    assert report["links_unresolved"] == [] and report["out_of_scope"] == []
    assert report["requests"] == 20  # 2 listings, 4 catalogues, 7 probes, 7 cubes
    assert report["areas_unsupported"] == {}


def test_rerun_is_byte_identical_and_unchanged(data, tmp_path):
    before = stable(data)
    manifest = (data / MANIFEST).read_bytes()

    result = execute(RecordedClient(), data, tmp_path / "rerun")

    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 7
    assert (data / MANIFEST).read_bytes() == manifest
    assert stable(data) == before


def test_failing_indicator_is_withheld_then_restored(data, tmp_path):
    source = RecordedClient()
    source.failing = {source.cube_key("GONV", "G082")}
    before = published(data)["sv/GONV|G082"]

    result = execute(source, data, tmp_path / "down")

    assert result["status"] == "completed_with_errors"
    assert resources(result) == [("endpoint", "sv/GONV|G082")]
    assert result["counts"]["withheld"] == 1
    assert "sv/GONV|G082" not in published(data)
    assert load_state(data)["datasets"]["sv/GONV|G082"]["status"] == "withheld"
    assert published(data, include_withheld=True)["sv/GONV|G082"] == before
    assert sum(k == source.cube_key("GONV", "G082") for k in source.requests) == 2

    result = execute(RecordedClient(), data, tmp_path / "back")

    assert result["counts"]["restored"] == 1
    assert published(data)["sv/GONV|G082"] == before


def test_unreachable_area_is_one_scope_error(data, tmp_path):
    source = RecordedClient()
    source.failing = {source.catalogue_key("NRAA")}

    result = execute(source, data, tmp_path / "area-down")

    assert resources(result) == [("endpoint", "*/NRAA")]
    assert result["counts"]["withheld"] == 2
    assert set(published(data)) == BOOTSTRAP - {"sv/NRAA|NRAA_1", "sv/NRAA|NRAA_2"}
    assert sum(k == source.catalogue_key("NRAA") for k in source.requests) == 2
    state = load_state(data)["datasets"]
    assert state["sv/NRAA|NRAA_1"]["error"]["resource"] == "*/NRAA"


@pytest.mark.parametrize("recovered", [False, True])
def test_error_telemetry_is_visible_before_next_sibling_and_retry_can_recover(
    data, tmp_path, monkeypatch, recovered
):
    import json

    from apps.harvest.metadata_adapters.dataanalys.api import DATA_URL

    progress = tmp_path / "telemetry" / "progress.json"
    monkeypatch.setenv("HARVEST_PROGRESS", str(progress))

    class ObservedClient(RecordedClient):
        def __init__(self):
            super().__init__()
            self.failed_key = self.cube_key("GONV", "G082")
            self.next_key = self.cube_key("GONV", "G013")
            self.attempts = 0
            self.observed = False

        async def post_json(self, url, body):
            from tests.support.dataanalys import post_key

            key = post_key(url, body)
            if key == self.failed_key:
                self.attempts += 1
                if not recovered or self.attempts == 1:
                    raise Unreachable("synthetic temporary outage")
            if key == self.next_key and self.attempts == 1:
                events = [
                    json.loads(line)
                    for line in progress.with_suffix(".errors.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                assert events[-1]["resource"] == "sv/GONV|G082"
                assert events[-1]["stage"] == "endpoint"
                assert events[-1]["url"] == DATA_URL
                assert json.loads(progress.read_text(encoding="utf-8"))["failed"] == 1
                self.observed = True
            return await super().post_json(url, body)

    source = ObservedClient()
    result = execute(source, data, tmp_path / "observed", areas=["GONV"])

    assert source.observed
    assert result["counts"]["failed"] == (0 if recovered else 1)
    assert result["status"] == ("success" if recovered else "completed_with_errors")
    events = [
        json.loads(line)
        for line in progress.with_suffix(".errors.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(events) == (1 if recovered else 2)  # attempts only; no final duplicate
    assert json.loads(progress.read_text(encoding="utf-8"))["failed"] == (
        0 if recovered else 1
    )
    if recovered:
        assert result["errors"] == []
        assert "sv/GONV|G082" in published(data)
    else:
        assert result["errors"][0]["url"] == DATA_URL


def test_unreadable_cube_is_stored_and_fails_at_parse(data, tmp_path):
    from apps.parsing.build import build

    source = RecordedClient()
    key = source.cube_key("GONV", "G200")
    source.responses[key]["body"]["result"][0]["data"][0]["Indelning_typ"] = "Kommun"

    result = execute(source, data, tmp_path / "bad")

    assert result["status"] == "success"  # the harvest stores what was returned
    assert result["counts"]["removed"] == 0
    parsed = build("dataanalys", data.parent, tmp_path / "parsed", providers=[PROVIDER])
    assert [e["key"] for e in parsed["errors"]] == [stored_key("sv/GONV|G200")]
    assert parsed["counts"]["parsed"] == 6


def test_malformed_row_does_not_stop_later_rows_and_duplicates_are_quarantined(
    data, tmp_path
):
    source = apply_synthetic(RecordedClient(), "malformed_row", "duplicate_row")
    before = published(data)

    result = execute(source, data, tmp_path / "rows")

    assert resources(result) == [
        ("discovery", "catalogue/GONV/0"),
        ("metadata", "sv/GONV|G200"),
    ]
    assert "quarantined" in result["errors"][1]["error"]
    assert result["counts"]["unchanged"] == 6  # every later row was processed
    assert result["counts"]["removed"] == 0
    assert published(data) == before  # the duplicate keeps its accepted document


def test_whole_listing_failure_preserves_documents_and_records_scope_error(
    data, tmp_path
):
    source = RecordedClient()
    source.failing = {
        k for k in source.responses if k.startswith("GET ") and "/chart/" in k
    }
    before = stable(data)

    result = execute(source, data, tmp_path / "out")

    assert result["status"] == "completed_with_errors"
    assert resources(result) == [("discovery", "charts")]
    assert any("/dashboard/" in request for request in source.requests)
    assert stable(data) == before


def test_dashboard_failure_still_rebuilds_independent_indicators(data, tmp_path):
    from apps.harvest.metadata_adapters.dataanalys.api import (
        DASHBOARD_COLUMNS,
        DASHBOARD_LIST_URL,
        list_query,
    )
    from tests.support.dataanalys import get_key

    source = RecordedClient()
    listing = source.listing("dashboard")
    original = list(listing["result"])
    listing["result"] = (original * 10)[:100]
    listing["count"] = 101
    source.failing.add(
        get_key(DASHBOARD_LIST_URL, {"q": list_query(DASHBOARD_COLUMNS, 1)})
    )
    before = published(data)

    result = execute(source, data, tmp_path / "dashboard-down")

    assert result["status"] == "completed_with_errors"
    assert ("discovery", "dashboards") in resources(result)
    assert result["counts"]["rebuilt"] + result["counts"]["unchanged"] > 0
    assert published(data) == before


def test_later_chart_page_failure_retains_first_page_and_processes_dashboards(client):
    from apps.harvest.metadata_adapters.dataanalys.api import (
        CHART_COLUMNS,
        CHART_LIST_URL,
        list_query,
    )
    from apps.harvest.metadata_adapters.dataanalys.workflow import discover
    from tests.support.dataanalys import get_key

    listing = client.listing("chart")
    original = list(listing["result"])
    listing["result"] = (original * 4)[:100]
    listing["count"] = 101
    failed = get_key(CHART_LIST_URL, {"q": list_query(CHART_COLUMNS, 1)})
    client.failing.add(failed)

    discovery = asyncio.run(discover(client))

    assert discovery.charts_listed == 100
    assert not discovery.charts_complete
    assert discovery.dashboards_complete
    assert set(discovery.areas) == {"GONV", "LRHST", "NRAA"}
    assert [(r, type(e)) for r, e in discovery.problems] == [("charts", Unreachable)]


@pytest.mark.parametrize("bounded", [False, True])
def test_incomplete_discovery_and_bounded_runs_never_remove_out_of_scope(
    data, tmp_path, bounded
):
    source = RecordedClient()
    source.responses[source.cube_key("NRAA", "NRAA_2")]["body"]["result"][0][
        "data"
    ] = []
    source.catalogue_rows("GONV")[:] = [
        r for r in source.catalogue_rows("GONV") if r["MBusinessKey"] != "G200"
    ]
    if not bounded:
        source.listing("chart")["count"] += 1

    result = execute(
        source,
        data,
        tmp_path / "preserve",
        **({"areas": ["GONV", "NRAA"]} if bounded else {}),
    )

    assert result["counts"]["removed"] == 0
    assert set(store(data).entries()) == {stored_key(k) for k in BOOTSTRAP}


def test_removal_only_in_an_unbounded_run_with_a_complete_chart_list(data, tmp_path):
    def without_nraa():
        source = RecordedClient()
        listing = source.listing("chart")
        listing["result"] = [
            r
            for r in listing["result"]
            if "NRAA" not in (r["datasource_name_text"] or "")
        ]
        listing["count"] = len(listing["result"])
        return source

    execute(without_nraa(), data, tmp_path / "bounded", areas=["GONV"])
    assert set(published(data)) == BOOTSTRAP

    incomplete = without_nraa()
    incomplete.listing("chart")["count"] += 1
    result = execute(incomplete, data, tmp_path / "incomplete")
    assert ("discovery", "charts") in resources(result)
    assert set(published(data)) == BOOTSTRAP

    result = execute(without_nraa(), data, tmp_path / "removed")
    assert result["counts"]["removed"] == 2
    assert set(published(data)) == BOOTSTRAP - {"sv/NRAA|NRAA_1", "sv/NRAA|NRAA_2"}
    assert stored_key("sv/NRAA|NRAA_1") not in store(data).entries()
    assert store(data).get_shared(PROVIDER_CODE, "catalogue/NRAA.json") is None
    assert store(data).get_shared(PROVIDER_CODE, "columns/NRAA.json") is None


def test_indicator_dropped_from_a_complete_catalogue_is_removed(data, tmp_path):
    def without_g200():
        source = RecordedClient()
        rows = source.catalogue_rows("GONV")
        rows[:] = [r for r in rows if r["MBusinessKey"] != "G200"]
        return source

    execute(without_g200(), data, tmp_path / "bounded", indicators=["G082"])
    assert "sv/GONV|G200" in published(data)

    result = execute(without_g200(), data, tmp_path / "removed")
    assert result["counts"]["removed"] == 1
    assert "sv/GONV|G200" not in published(data)


def test_bounded_runs_touch_only_the_selection(data, tmp_path):
    later = NOW + timedelta(days=1)
    source = RecordedClient()

    result = execute(source, data, tmp_path / "area", clock=later, areas=["nraa"])

    state = load_state(data)["datasets"]
    assert {k for k, v in state.items() if v["checked_at"] == later.isoformat()} == {
        "sv/NRAA|NRAA_1",
        "sv/NRAA|NRAA_2",
    }
    assert result["requests"] == 7  # 2 listings, catalogue, 2 probes, 2 cubes

    even_later = later + timedelta(days=1)
    source = RecordedClient()
    result = execute(
        source,
        data,
        tmp_path / "one",
        clock=even_later,
        indicators=["GONV|G082", "lrhst09"],
    )
    state = load_state(data)["datasets"]
    assert {
        k for k, v in state.items() if v["checked_at"] == even_later.isoformat()
    } == {
        "sv/GONV|G082",
        "sv/LRHST|LRHST09",
    }
    assert result["status"] == "success"
    assert sum('row_limit":20000' in k for k in source.requests) == 2


def test_unknown_selection_is_reported(data, tmp_path):
    result = execute(
        RecordedClient(),
        data,
        tmp_path / "unknown",
        areas=["GONV", "XYZ"],
        indicators=["G082", "G999"],
    )

    assert resources(result) == [("discovery", "xyz"), ("discovery", "g999")]
    assert all("Unknown" in e["error"] for e in result["errors"])


def test_indicator_without_riket_or_region_rows_is_stored_not_dismissed(data, tmp_path):
    from apps.parsing.build import build

    source = RecordedClient()
    source.responses[source.cube_key("NRAA", "NRAA_2")]["body"]["result"][0][
        "data"
    ] = []

    result = execute(source, data, tmp_path / "scope")

    assert result["status"] == "success"
    assert result["out_of_scope"] == ["sv/NRAA|NRAA_2"]
    assert result["counts"]["out_of_scope"] == 1
    assert result["counts"]["removed"] == 0
    assert store(data).get(stored_key("sv/NRAA|NRAA_2"))["responses"]["cube"]
    parsed = build("dataanalys", data.parent, tmp_path / "parsed", providers=[PROVIDER])
    assert [e["key"] for e in parsed["errors"]] == [stored_key("sv/NRAA|NRAA_2")]
    assert "out of scope" in parsed["errors"][0]["error"]
    assert parsed["counts"]["parsed"] == 6


def test_incomplete_area_keeps_its_documents(data, tmp_path):
    source = RecordedClient()
    listing = source.listing("chart")
    listing["result"] = [
        r
        for r in listing["result"]
        if r["datasource_name_text"] != "semantic.semantic_VAI_NRAA_Trender"
    ]
    listing["count"] = len(listing["result"])

    result = execute(source, data, tmp_path / "half")

    assert result["areas_incomplete"] == {"NRAA": "no Trender table listed"}
    assert resources(result) == [("metadata", "*/NRAA")]
    assert set(published(data)) == BOOTSTRAP


def test_values_table_outside_the_model(data, tmp_path):
    """Synthetic: NRAA's values table answers as if it had no indicator column, as
    NPS's does live (it has ``Mbusinesskey`` and profession breakdowns)."""

    def unsupported():
        source = RecordedClient()
        for key in source.keys(datasource=164, kind="probe"):
            if '"MBusinessKey"' in key:
                source.responses[key] = {
                    "status": 400,
                    "text": '{"message":"Error: Columns missing in dataset: '
                    "['MBusinessKey', 'GroupTypeName']\"}",
                }
        return source

    fresh = execute(unsupported(), tmp_path / "fresh", tmp_path / "fresh-out")
    assert fresh["status"] == "success"
    assert set(fresh["areas_unsupported"]) == {"NRAA"}
    assert "none of ['Matt_id', 'MBusinessKey']" in fresh["areas_unsupported"]["NRAA"]

    result = execute(unsupported(), data, tmp_path / "known")
    assert resources(result) == [("metadata", "*/NRAA")]
    assert set(published(data)) == BOOTSTRAP


def test_links_and_unpublished_dashboards(data, tmp_path):
    source = apply_synthetic(RecordedClient(), "broken_href", "unpublished_dashboard")

    result = execute(source, data, tmp_path / "links")

    assert result["status"] == "success"
    documents = published(data)
    native = documents["sv/GONV|G013"]["extension"]["socialstyrelsen_analys"]
    assert native["link_resolution"] == "chart_filter"
    native = documents["sv/GONV|G033"]["extension"]["socialstyrelsen_analys"]
    assert native["dashboard_slug"] == "G133_Paverkbar_slv_hjartsvikt"
    assert result["counts"]["rebuilt"] == 2


def test_stale_artifact_rejected(data, tmp_path):
    from apps.harvest.metadata_adapters.dataanalys.workflow import collect

    start(RecordedClient(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])


def test_output_inside_published_data_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        start(RecordedClient(), tmp_path, tmp_path / "out")

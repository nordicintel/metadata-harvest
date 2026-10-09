"""Weekly analysverktyg lifecycle against recorded answers."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from metadata_harvest.adapters.dataanalys.client import Unreachable
from metadata_harvest.adapters.dataanalys.state import (
    collection,
    load_documents,
    load_state,
)
from tests.dataanalys.conftest import PROVIDER, RecordedClient, apply_synthetic

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
    from metadata_harvest.adapters.dataanalys.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from metadata_harvest.adapters.dataanalys.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data):
    return load_documents(data, ["sv"])


def resources(result):
    return [(e["stage"], e["resource"]) for e in result["errors"]]


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    result = execute(RecordedClient(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(published(root)) == 7
    return root


def test_bootstrap_publishes_valid_documents(data, tmp_path):
    from metadata_harvest.storage import read
    from metadata_harvest.validation import validate_metadata

    documents = published(data)
    assert set(documents) == BOOTSTRAP
    for document in documents.values():
        validate_metadata(document, adapter="dataanalys")
        assert document["extension"]["socialstyrelsen_analys"]["source_record"]
    report = read(data / "reports/latest.json")
    assert report["areas"] == ["GONV", "LRHST", "NRAA"]
    assert report["unsupported_datasources"] == [
        "MART.vai_cancer_forekomst_femar_total"
    ]
    assert report["links_unresolved"] == [] and report["out_of_scope"] == []
    assert report["requests"] == 20  # 2 listings, 4 catalogues, 7 probes, 7 cubes
    assert report["areas_unsupported"] == {}


def test_rerun_is_byte_identical_and_unchanged(data, tmp_path):
    before = (data / collection("sv")).read_bytes()
    state = (data / "state/current.jsonl.gz").read_bytes()

    result = execute(RecordedClient(), data, tmp_path / "rerun")

    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 7
    assert (data / collection("sv")).read_bytes() == before
    assert (data / "state/current.jsonl.gz").read_bytes() == state


def test_failing_indicator_is_withheld_then_restored(data, tmp_path):
    source = RecordedClient()
    source.failing = {source.cube_key("GONV", "G082")}
    before = published(data)["sv/GONV|G082"]

    result = execute(source, data, tmp_path / "down")

    assert result["status"] == "completed_with_errors"
    assert resources(result) == [("endpoint", "sv/GONV|G082")]
    assert result["counts"]["withheld"] == 1
    assert "sv/GONV|G082" not in published(data)
    assert load_state(data)["withheld"]["sv/GONV|G082"] == before
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


def test_metadata_failure_keeps_the_document_until_it_is_stale(data, tmp_path):
    source = RecordedClient()
    key = source.cube_key("GONV", "G200")
    source.responses[key]["body"]["result"][0]["data"][0]["Indelning_typ"] = "Kommun"
    before = published(data)["sv/GONV|G200"]

    result = execute(source, data, tmp_path / "bad")

    assert resources(result) == [("metadata", "sv/GONV|G200")]
    assert published(data)["sv/GONV|G200"] == before
    assert result["counts"]["removed"] == 0

    execute(source, data, tmp_path / "stale", clock=NOW + timedelta(days=61))

    assert "sv/GONV|G200" not in published(data)
    assert load_state(data)["datasets"]["sv/GONV|G200"]["withheld_reason"] == "stale"


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


def test_whole_listing_failure_raises_and_publishes_nothing(data, tmp_path):
    source = RecordedClient()
    source.failing = {
        k for k in source.responses if k.startswith("GET ") and "/chart/" in k
    }
    before = (data / collection("sv")).read_bytes()

    with pytest.raises(Unreachable):
        start(source, data, tmp_path / "out")

    assert (data / collection("sv")).read_bytes() == before


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


def test_indicator_without_riket_or_region_rows_is_out_of_scope(data, tmp_path):
    source = RecordedClient()
    source.responses[source.cube_key("NRAA", "NRAA_2")]["body"]["result"][0][
        "data"
    ] = []

    result = execute(source, data, tmp_path / "scope")

    assert result["status"] == "success"
    assert result["out_of_scope"] == ["sv/NRAA|NRAA_2"]
    assert result["counts"]["out_of_scope"] == 1
    assert "sv/NRAA|NRAA_2" not in published(data)


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
    from metadata_harvest.adapters.dataanalys.workflow import collect

    start(RecordedClient(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])


def test_output_inside_published_data_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        start(RecordedClient(), tmp_path, tmp_path / "out")

"""Discovery, catalogue, cube and document mapping against recorded answers."""

import asyncio

import pytest

from metadata_harvest.adapters.dataanalys import api, source
from metadata_harvest.adapters.dataanalys.mapping import (
    build_dataset,
    indicator_label,
)
from metadata_harvest.adapters.dataanalys.source import (
    Discovery,
    add_charts,
    add_dashboards,
    parse_catalogue,
    parse_cube,
    resolve_link,
)
from metadata_harvest.adapters.dataanalys.workflow import discover, fetch_metadata
from metadata_harvest.validation import validate_metadata
from tests.dataanalys.conftest import RecordedClient, apply_synthetic

GONV_COLUMNS = {
    "indicator": "Matt_id",
    "level": "Indelning_typ",
    "code": "Indelning_etikett",
    "sex": "Kon",
    "age": "Alder",
    "period_type": "Matperiod_typ",
    "period": "Matperiod_char",
    "period_date": "Matperiod_datum",
    "value": "Varde",
    "status": "Kommentar",
}


def discovered(client=None) -> Discovery:
    return asyncio.run(discover(client or RecordedClient()))


def documents(area, *codes, client=None):
    return asyncio.run(
        fetch_metadata(
            {"area": area, "indicators": list(codes)}, client=client or RecordedClient()
        )
    )


def test_chart_list_gives_area_tables_and_unsupported_datasources():
    result = discovered()

    assert sorted(result.areas) == ["GONV", "LRHST", "NRAA"]
    gonv = result.areas["GONV"]
    assert (gonv.catalogue_id, gonv.values_id) == (160, 159)
    assert gonv.catalogue_dataset == "semantic.semantic_VAI_GONV_Landningssida"
    assert gonv.values_dataset == "semantic.semantic_VAI_GONV_Trender"
    assert result.unsupported == ["MART.vai_cancer_forekomst_femar_total"]
    assert result.incomplete == {}
    assert result.charts_complete and result.dashboards_complete
    assert result.filters["G013"] >= {115, 149}


def test_schema_prefix_is_case_insensitive_and_single_tables_are_incomplete():
    """NRTV's catalogue is listed as ``Semantic.semantic_VAI_NRTV_Landningssida``."""
    result = Discovery()
    add_charts(
        result,
        [
            {
                "id": 1,
                "datasource_id": 147,
                "datasource_name_text": "Semantic.semantic_VAI_NRTV_Landningssida",
                "dashboards": [],
            },
            {
                "id": 2,
                "datasource_id": 146,
                "datasource_name_text": "semantic.semantic_VAI_NRTV_Trender",
                "dashboards": [],
            },
            {
                "id": 3,
                "datasource_id": 150,
                "datasource_name_text": "semantic.semantic_VAI_XX_Trender",
                "dashboards": [],
            },
            {"id": "bad", "datasource_name_text": 7},
        ],
    )

    assert result.areas["NRTV"].catalogue_id == 147
    assert result.incomplete == {"XX": "no Landningssida table listed"}
    assert [resource for resource, _ in result.problems] == ["charts/#3"]


def test_dashboards_without_slug_are_skipped():
    result = discovered()

    assert 212 not in result.dashboards  # "EG - Regioner" has slug null
    assert result.dashboards[191].slug == "adhd_autism"
    assert {"OMR: NRAA", "Start"} <= result.dashboards[191].tags


def test_lists_are_paged_until_the_count(monkeypatch):
    client = RecordedClient()
    charts = client.listing("chart")

    class Paging:
        request_count = 0

        async def get_json(self, url, params=None):
            if "/dashboard/" in url:
                return client.listing("dashboard")
            page = int(params["q"].split("page:")[1].split(",")[0])
            return {
                "count": charts["count"],
                "result": charts["result"][page * 10 : page * 10 + 10],
            }

    monkeypatch.setattr(api, "PAGE_SIZE", 10)
    monkeypatch.setattr("metadata_harvest.adapters.dataanalys.workflow.PAGE_SIZE", 10)

    result = asyncio.run(discover(Paging()))

    assert result.charts_listed == charts["count"] == 33
    assert result.charts_complete
    assert sorted(result.areas) == ["GONV", "LRHST", "NRAA"]


def test_short_listing_is_incomplete():
    client = RecordedClient()
    client.listing("chart")["count"] += 1

    result = discovered(client)

    assert not result.charts_complete
    assert sorted(result.areas) == ["GONV", "LRHST", "NRAA"]


def test_g082_document():
    [document] = documents("GONV", "G082")

    validate_metadata(document, adapter="dataanalys")
    assert document["label"] == "Kontinuitetsindex primärvårdsläkare, kronisk sjukdom"
    assert document["updated"] == "2026-04-07"
    assert "source" not in document  # DataSource is empty for G082
    assert document["id"] == ["Region", "Kon", "Alder", "Tid", "ContentsCode"]
    assert document["size"] == [22, 3, 1, 4, 1]
    assert document["role"] == {
        "geo": ["Region"],
        "time": ["Tid"],
        "metric": ["ContentsCode"],
    }
    region = document["dimension"]["Region"]
    assert region["category"]["index"][:3] == ["Riket", "Blekinge", "Dalarna"]
    assert region["category"]["index"][-2:] == ["Örebro", "Östergötland"]
    assert region["extension"]["nordicintel"] == {
        "elimination": True,
        "elimination_value": "Riket",
    }
    assert region["extension"]["socialstyrelsen_analys"]["categories"]["Skåne"] == {
        "level": "Region"
    }
    sex = document["dimension"]["Kon"]
    assert sex["category"]["index"] == ["Kön, totalt", "Kvinnor", "Män"]
    assert sex["extension"]["nordicintel"]["elimination_value"] == "Kön, totalt"
    age = document["dimension"]["Alder"]
    assert age["category"]["index"] == ["Ålder, totalt"]
    assert age["extension"]["nordicintel"]["elimination_value"] == "Ålder, totalt"
    assert document["dimension"]["Tid"]["category"]["index"] == [
        "2022",
        "2023",
        "2024",
        "2025",
    ]
    assert document["dimension"]["Tid"]["extension"]["nordicintel"] == {
        "elimination": False
    }
    assert document["dimension"]["ContentsCode"]["category"] == {
        "index": ["G082"],
        "label": {"G082": "Kontinuitetsindex primärvårdsläkare, kronisk sjukdom"},
        "unit": {"G082": {"label": "Index"}},
    }
    assert document["note"][:2] == [
        "Önskat värde: Högt.",
        "Indikatortyp: Kärnindikator.",
    ]
    assert "(kolumnen Kommentar)" in document["note"][2]
    assert document["value"] == []
    shared = document["extension"]["nordicintel"]
    assert shared == {
        "provider_code": "socialstyrelsen_analys",
        "dataset_code": "GONV|G082",
        "language": "sv",
        "source_url": "https://dataanalys.socialstyrelsen.se/superset/dashboard/"
        "G082_primarvardlakare_kronisksjukdom/",
        "doc_url": "https://www.socialstyrelsen.se/statistik-och-data/indikatorer/"
        "indikatorbibliotek/kontinuitetsindex-primarvardslakare-kronisk-sjukdom",
        "metadata_url": "https://dataanalys.socialstyrelsen.se/api/v1/dashboard/213/"
        "datasets",
        "data_url": "https://dataanalys.socialstyrelsen.se/api/v1/chart/data",
        "description": shared["description"],
        "time_unit": "annual",
        "first_period": "2022",
        "last_period": "2025",
        "subject": {"code": "GONV", "label": "God och nära vård"},
    }
    assert shared["description"].startswith("Kontinuitetsindex för patienter")
    native = document["extension"]["socialstyrelsen_analys"]
    assert set(native) == {
        "area",
        "indicator",
        "values_dataset_id",
        "values_dataset",
        "catalogue_dataset_id",
        "catalogue_dataset",
        "columns",
        "dashboard_id",
        "dashboard_slug",
        "link_resolution",
        "measure_period_type",
        "updated_original",
        "source_record",
    }
    assert native["columns"] == {
        "Region": {
            "level": "Indelning_typ",
            "code": "Indelning_etikett",
            "levels": ["Riket", "Region"],
        },
        "Kon": "Kon",
        "Alder": "Alder",
        "Tid": "Matperiod_char",
        "ContentsCode": "Matt_id",
        "value": "Varde",
        "status": "Kommentar",
    }
    assert (native["dashboard_id"], native["link_resolution"]) == (213, "href")
    assert native["measure_period_type"] == "1_ar"
    assert native["updated_original"] == 1775520000000.0
    assert native["source_record"]["MBusinessKey"] == "G082"
    assert native["source_record"]["DataGenerationDate"] == 1775520000000.0


def test_catalogue_without_mtypename_is_retried_without_it():
    """LRHST's catalogue lacks MTypeName (recorded HTTP 400, then the retry)."""
    client = RecordedClient()

    [document] = documents("LRHST", "LRHST09", client=client)

    catalogue = [k for k in client.requests if '"id":158' in k]
    assert len(catalogue) == 2 and '"MTypeName"' not in catalogue[1]
    validate_metadata(document, adapter="dataanalys")
    assert document["label"] == "Dödlighet efter hjärtinfarkt"  # from MName
    assert (
        "MTypeName"
        not in document["extension"]["socialstyrelsen_analys"]["source_record"]
    )


def test_native_column_names_are_probed():
    """NRAA's and LRHST's values tables use native names (recorded HTTP 400s)."""
    [nraa] = documents("NRAA", "NRAA_1")
    [lrhst] = documents("LRHST", "LRHST09")

    assert nraa["extension"]["socialstyrelsen_analys"]["columns"] == {
        "Region": {
            "level": "GroupTypeName",
            "code": "GroupName",
            "levels": ["Riket", "Region"],
        },
        "Kon": "Gender",
        "Alder": "AgeLabel",
        "Tid": "MeasurementPeriod",
        "ContentsCode": "MBusinessKey",
        "value": "Value",
        "status": "Commentary",
    }
    assert "(kolumnen Commentary)" in nraa["note"][-1]
    # LRHST has no AgeLabel (its age is "Age") and no period type: four probes.
    assert lrhst["extension"]["socialstyrelsen_analys"]["columns"]["Alder"] == "Age"
    age = lrhst["dimension"]["Alder"]
    assert age["category"]["index"] == ["20 år eller äldre"]
    assert age["extension"]["nordicintel"] == {"elimination": False}  # no total
    assert "time_unit" not in lrhst["extension"]["nordicintel"]
    assert "measure_period_type" not in lrhst["extension"]["socialstyrelsen_analys"]


def test_broken_catalogue_link_falls_back_to_the_area_start_page():
    """Every NRAA catalogue link points to a dashboard that does not exist."""
    [document] = documents("NRAA", "NRAA_1")

    native = document["extension"]["socialstyrelsen_analys"]
    assert native["link_resolution"] == "area_overview"
    assert native["dashboard_slug"] == "adhd_autism"
    assert document["extension"]["nordicintel"]["subject"] == {
        "code": "NRAA",
        "label": "ADHD/Autism i Sverige",
    }


def test_broken_link_prefers_the_most_specific_filtering_dashboard():
    """Synthetic: G013's link is broken; charts on 115 (regional profile, many
    indicators) and 149 (G013 only) filter Matt_id G013."""
    client = apply_synthetic(RecordedClient(), "broken_href")

    [document] = documents("GONV", "G013", client=client)

    native = document["extension"]["socialstyrelsen_analys"]
    assert (native["link_resolution"], native["dashboard_id"]) == ("chart_filter", 149)
    assert document["extension"]["nordicintel"]["source_url"].endswith(
        "/superset/dashboard/G013_Oplan_aterinskrivning_aldre/"
    )


def test_unpublished_dashboard_is_not_a_source_url():
    client = apply_synthetic(RecordedClient(), "unpublished_dashboard")

    [document] = documents("GONV", "G033", client=client)

    native = document["extension"]["socialstyrelsen_analys"]
    assert native["link_resolution"] == "chart_filter"
    assert native["dashboard_slug"] == "G133_Paverkbar_slv_hjartsvikt"


def test_link_resolution_order():
    result = discovered()
    href = (
        '<a href="https://dataanalys.socialstyrelsen.se/superset/dashboard/{}/">x</a>'
    )

    assert (
        resolve_link(
            result, "GONV", "G082", href.format("G082_primarvardlakare_kronisksjukdom")
        ).resolution
        == "href"
    )
    assert resolve_link(result, "GONV", "G082", None).dashboard.id == 213
    assert resolve_link(result, "GONV", "G999", None).dashboard.slug == "GONV_Oversikt"
    assert resolve_link(result, "XX", "G999", None).resolution == "unresolved"
    other_host = '<a href="https://example.org/superset/dashboard/adhd_autism/">x</a>'
    assert resolve_link(result, "XX", "Y", other_host).resolution == "unresolved"
    result.dashboards_complete = False
    assert (
        resolve_link(
            result, "GONV", "G082", href.format("G082_primarvardlakare_kronisksjukdom")
        ).resolution
        == "href"
    )
    with pytest.raises(ValueError, match="incomplete"):
        resolve_link(result, "GONV", "G082", None)


def test_label_chain():
    href = '<a href="x">Delaktighet, primärvård. Index (0–100)</a>'

    assert indicator_label("G1", {"MTypeName": "Typ", "MName": "Namn"}) == "Typ"
    assert indicator_label("G1", {"MTypeName": " ", "MName": "Namn"}) == "Namn"
    assert (
        indicator_label("G1", {"HrefIndicatorDashboard": href, "Unit": "Index (0–100)"})
        == "Delaktighet, primärvård"
    )
    assert indicator_label("G1", {"HrefIndicatorDashboard": href}) == (
        "Delaktighet, primärvård. Index (0–100)"
    )
    assert indicator_label("G1", {}) == "G1"


def test_malformed_and_duplicate_catalogue_rows():
    rows = [
        {"MBusinessKey": None},
        {"MBusinessKey": "A1", "HrefIndicatorDashboard": None},
        {"MBusinessKey": "A|2"},
        {"MBusinessKey": "B1", "DataGenerationDate": "igår"},
        {"MBusinessKey": "C1", "Unit": 3},
        {"MBusinessKey": "D1", "DataGenerationDate": 1.0},
        {"MBusinessKey": "D1", "DataGenerationDate": 2.0},
        {"MBusinessKey": "E1"},
    ]

    catalogue = parse_catalogue(rows, ["MBusinessKey"])

    assert [code for code, _ in catalogue.rows] == ["A1", "E1"]
    assert catalogue.duplicates == ["D1"]
    assert [position for position, _ in catalogue.problems] == [0, 2, 3, 4]
    assert not catalogue.complete


def test_cube_parsing():
    row = {
        "Indelning_typ": "Riket",
        "Indelning_etikett": "Riket",
        "Kon": "Kön, totalt",
        "Alder": None,
        "Matperiod_typ": "1_ar",
        "Matperiod_char": "2024",
        "Matperiod_datum": 2.0,
    }
    later = {**row, "Matperiod_char": "2023-2025", "Matperiod_datum": 3.0}
    earlier = {
        **row,
        "Indelning_typ": "Region",
        "Indelning_etikett": "Uppsala",
        "Matperiod_char": "2025",
        "Matperiod_datum": 1.0,
        "Kon": "Män",
    }

    cube = parse_cube([later, row, earlier], GONV_COLUMNS)

    assert cube.regions == (("Riket", "Riket"), ("Uppsala", "Region"))
    assert cube.sexes == ("Kön, totalt", "Män")
    assert cube.ages is None  # all null: no age dimension
    assert cube.periods == ("2025", "2024", "2023-2025")  # by Matperiod_datum
    assert parse_cube([], GONV_COLUMNS) is None
    with pytest.raises(ValueError, match="unexpected level"):
        parse_cube([{**row, "Indelning_typ": "Kommun"}], GONV_COLUMNS)
    with pytest.raises(ValueError, match="two levels"):
        parse_cube([row, {**row, "Indelning_typ": "Region"}], GONV_COLUMNS)
    with pytest.raises(ValueError, match="nonblank"):
        parse_cube(
            [
                row,
                {
                    **row,
                    "Kon": None,
                    "Indelning_etikett": "X",
                    "Indelning_typ": "Region",
                },
            ],
            GONV_COLUMNS,
        )


def test_ages_sort_numerically_with_the_total_first():
    rows = [
        {
            "Indelning_typ": "Riket",
            "Indelning_etikett": "Riket",
            "Alder": age,
            "Matperiod_char": "2024",
        }
        for age in ("85 år eller äldre", "5-9 år", "Ålder, totalt", "65-69 år")
    ]

    cube = parse_cube(
        rows, {k: GONV_COLUMNS[k] for k in ("level", "code", "age", "period")}
    )

    assert cube.ages == ("Ålder, totalt", "5-9 år", "65-69 år", "85 år eller äldre")


def test_missing_columns_message():
    text = "{\"message\": \"Error: Columns missing in dataset: ['MTypeName', 'Unit']\"}"

    assert api.missing_columns(text) == ["MTypeName", "Unit"]
    assert api.missing_columns('{"message": "Not found"}') is None


def test_document_without_optional_fields_still_validates():
    result = discovered()
    area = result.areas["GONV"]
    cube = parse_cube(
        [
            {
                "Indelning_typ": "Region",
                "Indelning_etikett": "Uppsala",
                "Matperiod_char": "2024",
            }
        ],
        {k: GONV_COLUMNS[k] for k in ("indicator", "level", "code", "period", "value")},
    )

    document = build_dataset(
        area,
        "X1",
        {"MBusinessKey": "X1", "LinkIndicatorDescription": "/relativ/lank"},
        cube,
        {k: GONV_COLUMNS[k] for k in ("indicator", "level", "code", "period", "value")},
        source.Link("unresolved"),
        subject_label="GONV",
    )

    validate_metadata(document, adapter="dataanalys")
    assert "note" not in document and "updated" not in document
    assert document["label"] == "X1"
    assert document["dimension"]["Region"]["extension"]["nordicintel"] == {
        "elimination": False
    }
    shared = document["extension"]["nordicintel"]
    assert "source_url" not in shared and "doc_url" not in shared
    assert "metadata_url" not in shared


def test_dashboard_rows_are_validated():
    result = Discovery()
    add_dashboards(
        result,
        [
            {"id": 1, "slug": "a", "published": "yes"},
            {"id": 2, "slug": "b", "published": True, "tags": [{"name": "Start"}]},
        ],
    )

    assert [resource for resource, _ in result.problems] == ["dashboards/1"]
    assert result.dashboards[2].tags == frozenset({"Start"})

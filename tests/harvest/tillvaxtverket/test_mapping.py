"""Mapping of recorded Tillväxtverket responses to Dataset documents."""

import asyncio
import json
from pathlib import Path

import pytest

from apps.harvest.metadata_adapters.tillvaxtverket.source import (
    BY_NAME,
    COUNTRY,
    Conflict,
    parse_categories,
    parse_labels,
    parse_probe,
    parse_regions,
)
from apps.harvest.validation import validate_metadata
from tests.support.tillvaxtverket import (
    RECORDED,
    RecordedClient,
    distinct_key,
    probe_key,
)


def build(name, client=None):
    from apps.harvest.metadata_adapters.tillvaxtverket.workflow import fetch_metadata

    return asyncio.run(fetch_metadata(BY_NAME[name], client=client or RecordedClient()))


def regions_body(name="GuestNights_Capacity_Revenue_Year"):
    return RECORDED[
        distinct_key(
            name, "NIVA_NAMN", "LAN_KOD", "LAN_NAMN", "KOMMUN_KOD", "KOMMUN_NAMN"
        )
    ]


def test_revenue_year_document():
    document = build("GuestNights_Capacity_Revenue_Year")

    validate_metadata(document, adapter="tillvaxtverket")
    assert document["label"] == (
        "Logiintäkter, Gästnätter beläggning per år fördelat på Sverige och utland"
    )
    assert document["source"] == "Tillväxtverket"
    assert document["updated"] == "2026-09-28"
    assert document["id"] == ["Region", "Anlaggningstyp", "Tid", "ContentsCode"]
    assert document["size"] == [8, 6, 18, 9]
    assert document["role"] == {
        "time": ["Tid"],
        "geo": ["Region"],
        "metric": ["ContentsCode"],
    }
    assert document["value"] == [] and "status" not in document
    assert document["note"][:2] == [
        "Statistiken är preliminär under pågående år.",
        "I april publiceras föregående års slutgiltiga statistik.",
    ]


def test_region_hierarchy_and_suppressed_rows():
    region = build("GuestNights_Capacity_Revenue_Year")["dimension"]["Region"]
    category = region["category"]

    assert category["index"] == [
        "riket",
        "lan:01",
        "lan:03",
        "kommun:0117",
        "kommun:0180",
        "kommun:01:Sekretesskyddad",
        "kommun:0380",
        "kommun:03:Sekretesskyddad",
    ]
    assert category["label"]["riket"] == "Riket"
    assert category["label"]["lan:01"] == "Stockholm"
    assert category["label"]["kommun:0180"] == "Stockholm"
    assert category["label"]["kommun:01:Sekretesskyddad"] == "Sekretesskyddad"
    assert category["child"]["riket"] == ["lan:01", "lan:03"]
    assert category["child"]["lan:01"][-1] == "kommun:01:Sekretesskyddad"
    # The national rows' "Sekretesskyddad" filler is no category of its own.
    assert not any(code.startswith("lan:Sek") for code in category["index"])
    assert region["extension"] == {"nordicintel": {"elimination": False}}


def test_capacity_rows_use_riket_as_filler():
    regions = parse_regions(regions_body("GuestNights_Capacity_Year"))
    assert regions[0].code == "riket"
    assert [r.code for r in regions].count("riket") == 1


def test_daily_capacity_keeps_recorded_suppressed_county():
    fixture = json.loads(
        (Path("tests/fixtures/tillvaxtverket/suppressed_county.json")).read_text(
            encoding="utf-8"
        )
    )
    regions = parse_regions(fixture)

    assert [(region.code, region.label, region.parent) for region in regions] == [
        ("lan:Sekretesskyddad", "Sekretesskyddad", "riket")
    ]
    client = RecordedClient()
    key = distinct_key(
        "GuestNights_Capacity_Day",
        "NIVA_NAMN",
        "LAN_KOD",
        "LAN_NAMN",
        "KOMMUN_KOD",
        "KOMMUN_NAMN",
    )
    body = client.responses[key]
    body["rows"].extend(fixture["rows"])
    body["rowCount"] = len(body["rows"])
    document = build("GuestNights_Capacity_Day", client)
    validate_metadata(document, adapter="tillvaxtverket")
    category = document["dimension"]["Region"]["category"]
    assert category["label"]["lan:Sekretesskyddad"] == "Sekretesskyddad"
    assert "lan:Sekretesskyddad" in category["index"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"NIVA_NAMN": "Socken", "LAN_KOD": "01"}, "unknown region"),
        (
            {"NIVA_NAMN": "Kommun", "LAN_KOD": "Sekretesskyddad", "KOMMUN_KOD": "0117"},
            "unknown region",
        ),
    ],
)
def test_unknown_region_combinations_are_rejected(row, message):
    body = regions_body()
    body = {**body, "rows": [*body["rows"], row], "rowCount": body["rowCount"] + 1}
    with pytest.raises(ValueError, match=message):
        parse_regions(body)


def test_conflicting_region_labels_are_a_conflict():
    body = regions_body()
    original = next(r for r in body["rows"] if r["KOMMUN_KOD"] == "0180")
    row = {**original, "KOMMUN_NAMN": "Annat"}
    with pytest.raises(Conflict):
        parse_regions({**body, "rows": [*body["rows"], row], "rowCount": None})


def test_truncated_distinct_list_is_rejected():
    body = regions_body()
    with pytest.raises(ValueError, match="rows returned"):
        parse_regions({**body, "rowCount": body["rowCount"] + 1})


def test_accommodation_types_keep_the_suppressed_category():
    document = build("GuestNights_Capacity_Revenue_Year")
    types = document["dimension"]["Anlaggningstyp"]
    assert types["label"] == "Anläggningstyp"
    assert types["category"]["index"] == [
        "Camping",
        "Hotell",
        "Sekretesskyddad",
        "SoL",
        "Stugbyar",
        "Vandrarhem",
    ]
    assert any("Sekretesskyddad" in note for note in types["note"])


def test_measures_use_label_file_and_no_units():
    measures = build("GuestNights_Capacity_Revenue_Year")["dimension"]["ContentsCode"]
    category = measures["category"]
    assert category["index"][0] == "LOGIINTAKT"
    assert category["label"]["LOGIINTAKT"] == "Logiintäkt"
    assert category["label"]["ANKOMSTER_SVE"] == "Ankomster från Sverige"
    assert "unit" not in category
    assert "LOGIINTAKT" in measures["note"][0]


def test_country_dimension_with_groups():
    document = build("GuestNights_Country_Year")
    validate_metadata(document, adapter="tillvaxtverket")
    assert document["id"] == ["Region", "Anlaggningstyp", "Land", "Tid", "ContentsCode"]
    land = document["dimension"]["Land"]
    assert land["category"]["label"]["SE"] == "Sverige"
    assert land["category"]["label"]["OA"] == "Övriga Asien"
    assert land["extension"]["tillvaxtverket"]["categories"]["NZ"] == {
        "LANDGRP_SVUTL_NAMN": "Utland",
        "LANDGRP_GROV_NAMN": "Världen exkl. Europa",
        "LANDGRP_20_NAMN": "Övriga Oceanien",
        "LANDGRP_40_NAMN": "Nya Zeeland",
    }
    assert document["extension"]["nordicintel"]["time_unit"] == "annual"


def test_day_type_dimension_and_time_units():
    year = build("GuestNights_Capacity_Year")
    assert year["id"] == ["Region", "Anlaggningstyp", "Dagtyp", "Tid", "ContentsCode"]
    assert year["dimension"]["Dagtyp"]["category"]["index"] == ["Helg", "Vardag"]
    assert year["dimension"]["Dagtyp"]["label"] == "Vardag och helg"

    day = build("GuestNights_Capacity_Day")
    shared = day["extension"]["nordicintel"]
    assert "Dagtyp" not in day["id"]  # derived from the date there
    assert shared["time_unit"] == "daily"
    assert (shared["first_period"], shared["last_period"]) == (
        day["dimension"]["Tid"]["category"]["index"][0],
        day["dimension"]["Tid"]["category"]["index"][-1],
    )
    month = build("GuestNights_Country_Month")["extension"]["nordicintel"]
    assert (month["time_unit"], month["first_period"], month["last_period"]) == (
        "monthly",
        "2024-06",
        "2024-08",
    )


def test_identity_urls_contacts_and_native_namespace():
    document = build("GuestNights_Country_Year")
    shared = document["extension"]["nordicintel"]
    base = (
        "https://oppnadata.tillvaxtverket.se/api/api/query/inkvartering/data/"
        "GuestNights_Country_Year.cbase"
    )
    assert shared["provider_code"] == "tillvaxtverket"
    assert shared["dataset_code"] == "inkvartering/GuestNights_Country_Year"
    assert shared["language"] == "sv"
    assert shared["data_url"] == f"{base}?format=json"
    assert shared["metadata_url"] == f"{base}?format=json&limit=0"
    assert shared["source_url"] == (
        "https://data.europa.eu/data/datasets/"
        "https-oppnadata-tillvaxtverket-se-dataset_guestnights_country_year"
    )
    assert shared["doc_url"].startswith("https://tillvaxtverket.se/")
    assert shared["official_statistics"] is True
    assert "Sveriges officiella statistik" in shared["description"]
    # The DCAT e-mail is a placeholder: kept raw, never a contact attribute.
    assert shared["contacts"] == [
        {"name": "Patrik Svensson", "organization": "Tillväxtverket"}
    ]
    native = document["extension"]["tillvaxtverket"]
    assert native["dcat"]["contact_point"][0]["email"] == (
        "mailto:fornamn.efternamn@tillvaxtverket.se"
    )
    assert native["english_title"] == "Guest nights by country and year"
    assert native["dimension_columns"] == {
        "Anlaggningstyp": "ANLAGGNINGSTYP_NAMN",
        "Land": "LAND_KOD",
        "Tid": "AR",
    }
    assert native["updated_original"] == "2026-09-28, 20:12:04"
    columns = {c["name"]: c for c in native["columns"]}
    assert columns["ANTAL_GASTNATTER"] == {
        "name": "ANTAL_GASTNATTER",
        "type": "integer",
        "role": "measure",
        "label": "Antal gästnätter",
    }
    assert columns["LANDGRP_20_NAMN"]["role"] == "category_label"


def test_probe_rejects_changed_columns():
    spec = BY_NAME["GuestNights_Country_Year"]
    body = RECORDED[probe_key(spec.name)]
    loads = RECORDED[distinct_key(spec.name, "LOAD_DATE")]
    probe = parse_probe(spec, body, loads)
    assert probe.row_count == 476454
    assert probe.updated == "2026-09-28"
    changed = {**body, "columns": body["columns"][1:]}
    with pytest.raises(ValueError, match="missing \\['LOAD_DATE'\\]"):
        parse_probe(spec, changed, loads)


def test_label_file_parsing():
    labels = parse_labels(
        "Name;Type;Length;Label\r\nAR;Numeric;8;År\r\nX;Numeric;8;\r\n"
    )
    assert labels == {"AR": "År"}
    with pytest.raises(ValueError):
        parse_labels("a;b\n1;2\n")


def test_country_codes_with_two_names_conflict():
    rows = [
        {"LAND_KOD": "SE", "LAND_NAMN": "Sverige"},
        {"LAND_KOD": "SE", "LAND_NAMN": "Sweden"},
    ]
    with pytest.raises(Conflict):
        parse_categories(COUNTRY, {"rows": rows, "rowCount": 2})

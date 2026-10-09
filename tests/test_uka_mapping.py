"""Scanning recorded exports and building Dataset documents from them."""

from dataclasses import replace
from urllib.parse import urlencode

import pytest

from metadata_harvest.adapters.uka.mapping import Scan, build_dataset
from metadata_harvest.adapters.uka.source import (
    Entry,
    Indicator,
    parse_catalogue,
    parse_totals,
)
from metadata_harvest.validation import validate_metadata
from tests.uka_support import RECORDED, UKA, export_text

ENTRIES = {
    entry.id: entry
    for entry in parse_catalogue(RECORDED["responses"]["/api/indicators"])
}


def _export(indicator: int, period: str) -> tuple[str, str, str | None]:
    """URL, text and Last-Modified of a recorded export."""
    query = urlencode(
        sorted({"export": "all", "export_type": "csv", "year": period}.items())
    )
    url = RECORDED["responses"][f"/api/totals/{indicator}?{query}"]["url"]
    file = RECORDED["files"][url.removeprefix(UKA)]
    return url, export_text(file["file"].removesuffix(".gz")), file["last_modified"]


def _scan(indicator: int, *periods: str) -> Scan:
    scan = Scan()
    for period in periods:
        url, text, last_modified = _export(indicator, period)
        scan.add(period, text, url, last_modified)
    return scan


def _build(indicator_id: int, *periods: str) -> dict:
    indicator = parse_totals(
        RECORDED["responses"][f"/api/totals/{indicator_id}"], indicator_id
    )
    document = build_dataset(
        ENTRIES[indicator_id], indicator, _scan(indicator_id, *periods)
    )
    validate_metadata(document, adapter="uka")
    return document


def test_hierarchy_becomes_one_dimension_with_child_map():
    document = _build(15, "2025")

    assert document["id"] == ["År", "Lärosäte", "Examen", "Kön", "Åldersgrupp"]
    assert document["size"] == [1, 27, 20, 3, 4]
    examen = document["dimension"]["Examen"]
    assert examen["label"] == "Examen / Inriktning"
    category = examen["category"]
    assert category["index"][:4] == [
        "Total",
        "Förskollärarexamina",
        "Grundlärarexamina",
        "Grundlärarexamina > Grundlärarexamina, fritidshem",
    ]
    assert category["label"]["Total"] == "Total"
    assert category["label"]["Grundlärarexamina"] == "Grundlärarexamina"
    assert (
        category["label"]["Grundlärarexamina > Grundlärarexamina, fritidshem"]
        == "Grundlärarexamina, fritidshem"
    )
    assert category["child"]["Total"] == [
        "Förskollärarexamina",
        "Grundlärarexamina",
        "Lärarexamina",
        "Yrkeslärarexamina",
        "Ämneslärarexamina",
    ]
    assert category["child"]["Grundlärarexamina"] == [
        "Grundlärarexamina > Grundlärarexamina, fritidshem",
        "Grundlärarexamina > Grundlärarexamina, förskoleklass/årskurs 1-3",
        "Grundlärarexamina > Grundlärarexamina, årskurs 4-6",
    ]
    assert category["child"]["Ämneslärarexamina"] == [
        "Ämneslärarexamina > Ämneslärarexamina, gymnasieskolan",
        "Ämneslärarexamina > Ämneslärarexamina, åk 7-9",
    ]
    assert set(category["child"]) == {
        "Total",
        "Grundlärarexamina",
        "Lärarexamina",
        "Ämneslärarexamina",
    }
    assert examen["extension"] == {
        "nordicintel": {"elimination": True, "elimination_value": "Total"}
    }
    assert "child" not in document["dimension"]["Kön"]["category"]
    assert document["extension"]["uka"]["columns"]["Examen"] == [
        "Examen[0]",
        "Inriktning[1]",
    ]
    assert document["extension"]["uka"]["measure_columns"] == ["Antal"]


def test_quoted_export_with_two_flat_group_dimensions():
    document = _build(14, "2025")

    assert document["id"] == [
        "År",
        "Lärosäte",
        "Verksamhetsområde",
        "Finansiärsgrupp",
        "Kön",
        "Åldersgrupp",
    ]
    assert document["size"] == [1, 46, 7, 11, 1, 1]
    assert document["dimension"]["Verksamhetsområde"]["category"]["index"] == [
        "Total",
        "Beställd utbildning",
        "Forskning och utbildning på forskarnivå",
        "Fortlöpande miljöanalys",
        "Uppdragsforskning",
        "Uppdragsutbildning",
        "Utbildning på grundnivå och avancerad nivå",
    ]
    finansiar = document["dimension"]["Finansiärsgrupp"]["category"]
    assert finansiar["index"][:3] == ["Total", "Anslagsmedel", "EU"]
    assert finansiar["index"][-1] == "Övrigt"
    assert finansiar["label"]["Anslagsmedel"] == "Anslagsmedel"
    assert "child" not in finansiar
    assert document["dimension"]["Kön"]["category"]["index"] == ["Total"]
    assert document["extension"]["uka"]["measure_columns"] == ["Summa"]
    assert document["extension"]["uka"]["markers"] == []
    assert document["extension"]["nordicintel"]["subject"] == {"label": "Ekonomi"}


def test_renamed_measure_column_and_padded_periods():
    scan = _scan(7, "2024/25", "2017/18")
    indicator = parse_totals(RECORDED["responses"]["/api/totals/7"], 7)
    document = build_dataset(ENTRIES[7], indicator, scan)

    validate_metadata(document, adapter="uka")
    assert scan.measure_columns == {"AndelUbak": "2024/25", "Andel": "2017/18"}
    assert document["dimension"]["Läsår"]["category"]["index"] == [
        "2017/18",
        "2024/25",
    ]
    assert document["size"] == [2, 54, 3, 1]
    nordicintel = document["extension"]["nordicintel"]
    assert (nordicintel["first_period"], nordicintel["last_period"]) == (
        "2017/18",
        "2024/25",
    )
    uka = document["extension"]["uka"]
    assert uka["measure_columns"] == ["AndelUbak", "Andel"]
    assert uka["markers"] == [".."]
    assert set(uka["files"]) == {"2017/18", "2024/25"}
    assert uka["files"]["2017/18"]["last_modified"] == "Fri, 12 May 2023 07:56:44 GMT"
    assert document["updated"] == "2026-03-25"
    lärosäte = document["dimension"]["Lärosäte"]["category"]["index"]
    assert "Beckmans Designhögskola" in lärosäte  # only in 2017/18
    assert "Akademi för Ledarskap och Teologi" in lärosäte  # only in 2024/25


def test_semiannual_indicator_document():
    document = _build(97, "VT2026")
    url, _, _ = _export(97, "VT2026")

    assert document["version"] == "2.0"
    assert document["class"] == "dataset"
    assert document["label"] == "Antagna till yrkesexamensprogram"
    assert document["updated"] == "2026-03-31"
    assert document["id"] == ["Termin", "Lärosäte", "Program", "Kön", "Åldersgrupp"]
    assert document["size"] == [
        len(document["dimension"][code]["category"]["index"]) for code in document["id"]
    ]
    assert document["size"] == [1, 31, 34, 3, 4]
    assert document["role"] == {"time": ["Termin"]}
    assert document["value"] == []
    assert document["dimension"]["Termin"] == {
        "label": "Termin",
        "category": {"index": ["VT2026"]},
    }
    for code in ("Lärosäte", "Program", "Kön", "Åldersgrupp"):
        assert document["dimension"][code]["label"] == code
    assert document["dimension"]["Åldersgrupp"]["category"]["index"] == [
        "Total",
        "-24",
        "25-34",
        "35-",
    ]
    assert document["extension"]["nordicintel"] == {
        "provider_code": "uka",
        "dataset_code": "97",
        "language": "sv",
        "metadata_url": "https://statistik-api.uka.se/api/totals/97",
        "data_url": (
            "https://statistik-api.uka.se/api/totals/97?export=all&export_type=csv"
        ),
        "doc_url": (
            "https://www.uka.se/statistik-och-analys/hogskolan-i-siffror/"
            "beskrivningar-av-statistiken/utbildning-pa-grundniva-och-avancerad-niva"
            "?plusbox=12_4e1e1cc819b2c0041bac4_button"
        ),
        "description": (
            "Antal antagna till yrkesexamensprogram. Beskrivning av statistiken"
        ),
        "time_unit": "semiannual",
        "first_period": "VT2026",
        "last_period": "VT2026",
        "subject": {"label": "Utbildning på grundnivå och avancerad nivå"},
    }
    record = document["extension"]["uka"].pop("source_record")
    assert record["catalogue"] == {
        "id": 97,
        "name": "Antagna till yrkesexamensprogram",
        "group": "Utbildning på grundnivå och avancerad nivå",
        "most_recent_url": "https://statistik-api.uka.se/api/totals/97/?year=VT2026",
    }
    assert record["totals"]["name"] == "Antal antagna till yrkesexamensprogram"
    assert record["totals"]["periods"][-1] == "VT2026"
    assert document["extension"]["uka"] == {
        "indicator_name": "Antal antagna till yrkesexamensprogram",
        "measurement": "Antagna till yrkesexamensprogram",
        "current_year": "VT2026",
        "period_columns": ["Termin"],
        "measure_columns": ["Antal"],
        "columns": {
            "Lärosäte": ["Lärosäte"],
            "Program": ["Program"],
            "Kön": ["Kön"],
            "Åldersgrupp": ["Åldersgrupp"],
        },
        "markers": [],
        "conflicting_cells": {},
        "files": {
            "VT2026": {"url": url, "last_modified": "Tue, 31 Mar 2026 07:21:33 GMT"}
        },
    }


def test_time_units_follow_the_period_column():
    academic = _build(7, "2024/25")["extension"]["nordicintel"]
    calendar = _build(67, "2025")["extension"]["nordicintel"]

    assert academic["time_unit"] == "annual"
    assert calendar["time_unit"] == "annual"
    assert calendar["dataset_code"] == "67"


def test_every_period_of_an_indicator():
    indicator = parse_totals(RECORDED["responses"]["/api/totals/67"], 67)
    document = build_dataset(
        ENTRIES[67], indicator, _scan(67, *reversed(indicator.periods))
    )

    validate_metadata(document, adapter="uka")
    assert document["dimension"]["År"]["category"]["index"] == list(indicator.periods)
    assert document["updated"] == "2026-04-30"
    assert document["extension"]["nordicintel"]["first_period"] == "2012"


def test_periods_without_scanned_export_are_left_out():
    indicator = parse_totals(RECORDED["responses"]["/api/totals/97"], 97)
    assert len(indicator.periods) > 1

    document = build_dataset(ENTRIES[97], indicator, _scan(97, "VT2026"))

    assert document["dimension"]["Termin"]["category"]["index"] == ["VT2026"]
    assert document["size"][0] == 1
    assert list(document["extension"]["uka"]["files"]) == ["VT2026"]


def test_scan_records_rows_and_absent_cells():
    scan = _scan(97, "VT2026")

    assert scan.files["VT2026"].rows == 2541
    assert scan.markers == set()
    assert "Sjuksköterskeexamen" in scan.categories[1]


def test_layout_change_across_periods_raises():
    scan = _scan(7, "2024/25")
    url, text, last_modified = _export(97, "VT2026")

    with pytest.raises(ValueError, match="layout"):
        scan.add("2025", text, url, last_modified)


def test_row_for_another_period_raises():
    url, text, last_modified = _export(97, "VT2026")

    with pytest.raises(ValueError, match="row for 'VT2026'"):
        Scan().add("HT2025", text, url, last_modified)


def test_misspelled_period_header_is_accepted():
    scan = Scan()
    scan.add("2025", "År;Kön;Antal\n2025;Kvinnor;1\n", "u1", None)
    scan.add("2013", "wÅr;Kön;Antal\n2013;Män;2\n", "u2", None)

    assert scan.layout.period_column == "År"
    assert scan.period_columns == {"År": "2025", "wÅr": "2013"}
    assert list(scan.categories[0]) == ["Kvinnor", "Män"]


def test_conflicting_rows_are_recorded():
    text = "År;Kön;Antal\n2025;Kvinnor;1\n2025;Kvinnor;2\n2025;Män;3\n"
    scan = Scan()
    scan.add("2025", text, "https://x/a.csv", None)

    assert scan.conflicts == {"2025": [["Kvinnor"]]}
    assert scan.files["2025"].rows == 2


def test_identical_duplicate_rows_are_accepted():
    scan = Scan()
    scan.add("2025", "År;Kön;Antal\n2025;Kvinnor;1\n2025;Kvinnor;1\n", "u", None)

    assert scan.files["2025"].rows == 1
    assert scan.categories == [{"Kvinnor": ("Kvinnor",)}]


def test_build_without_exports_raises():
    indicator = parse_totals(RECORDED["responses"]["/api/totals/97"], 97)

    with pytest.raises(ValueError, match="no readable export"):
        build_dataset(ENTRIES[97], indicator, Scan())


def test_unparsable_last_modified_is_ignored():
    scan = _scan(97, "VT2026")
    scan.files["VT2026"] = replace(scan.files["VT2026"], last_modified="igår")
    indicator = parse_totals(RECORDED["responses"]["/api/totals/97"], 97)

    document = build_dataset(ENTRIES[97], indicator, scan)

    assert "updated" not in document
    validate_metadata(document, adapter="uka")


def test_geographic_breakdowns_get_the_geo_role():
    text = (
        "Termin;Lärosäte;Län[0];Kommun[1];Kön;Åldersgrupp;Antal\n"
        "HT2025;;Blekinge;Karlshamn;Total;Total;5\n"
        "HT2025;;Blekinge;;Total;Total;9\n"
    )
    scan = Scan()
    scan.add("HT2025", text, "https://x/a.csv", None)
    entry = Entry(27, "Högskolenybörjare per kommun", "Grupp", None)
    indicator = Indicator(27, "Nybörjare", None, None, "HT2025", ("HT2025",))
    document = build_dataset(entry, indicator, scan)

    assert document["role"] == {"time": ["Termin"], "geo": ["Län"]}
    assert document["dimension"]["Län"]["category"]["child"] == {
        "Blekinge": ["Blekinge > Karlshamn"]
    }
    validate_metadata(document, adapter="uka")

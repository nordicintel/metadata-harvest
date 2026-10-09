"""Mapping SDB subjects, variables and categories to Dataset documents."""

import json
from pathlib import Path

import pytest

from metadata_harvest.adapters.sdb.mapping import (
    Subject,
    build_dataset,
    build_dimension,
    html_to_text,
    time_unit_from_swedish_label,
)
from metadata_harvest.validation import validate_metadata

SDB_DATA = json.loads(
    (Path(__file__).parent / "fixtures" / "sdb.json").read_text(encoding="utf-8")
)


def dataset(subject: str, language: str = "sv", label: str = "Label") -> dict:
    return build_dataset(
        Subject(subject, label, "annual"),
        language,
        SDB_DATA["variables"][language][subject],
        SDB_DATA["categories"][language][subject],
    )


def test_amning_maps_variables_to_dimensions():
    document = dataset("amning", label="Amning, årsuppgifter")

    validate_metadata(document, adapter="sdb")
    assert document["id"] == ["region", "alder", "matt", "ar", "variabel"]
    assert document["size"] == [3, 3, 2, 3, 3]
    assert document["value"] == []
    assert document["role"] == {"time": ["ar"], "geo": ["region"], "metric": ["matt"]}
    region = document["dimension"]["region"]
    assert region["label"] == "Län"
    assert region["note"] == ["Avser hemortslän, dvs. där barnet är folkbokfört."]
    assert region["category"]["index"] == ["0", "1", "3"]
    assert region["category"]["label"]["0"] == "Riket"
    assert region["extension"] == {
        "socialstyrelsen": {
            "categories": {"0": {"kod": "00"}, "1": {"kod": "01"}, "3": {"kod": "03"}}
        }
    }
    variabel = document["dimension"]["variabel"]["category"]
    assert variabel["note"]["E"][0].startswith("Barn som har fått enbart bröstmjölk")


def test_amning_identity_urls_and_periods():
    nordicintel = dataset("amning")["extension"]["nordicintel"]

    years = sorted(
        str(item["id"]) for item in SDB_DATA["categories"]["sv"]["amning"]["ar"]
    )
    assert nordicintel == {
        "provider_code": "socialstyrelsen",
        "dataset_code": "amning",
        "language": "sv",
        "metadata_url": "https://sdb.socialstyrelsen.se/api/v1/sv/amning",
        "data_url": "https://sdb.socialstyrelsen.se/api/v1/sv/amning/resultat",
        "time_unit": "annual",
        "first_period": years[0],
        "last_period": years[-1],
    }


def test_diagnos_hierarchy_becomes_one_dimension_with_children():
    document = dataset("dodsorsaker")

    validate_metadata(document, adapter="sdb")
    diagnos = document["dimension"]["diagnos"]
    category = diagnos["category"]
    assert category["child"] == {
        "Spec1": ["Spec1_1"],
        "01": ["0101"],
        "0101": ["A00", "A01"],
    }
    assert category["label"]["01"] == "Vissa infektionssjukdomar och parasitsjukdomar"
    assert "\r" not in "".join(category["label"].values())
    assert category["note"]["Alk"][0].startswith("Alkoholrelaterad dödlighet")
    extras = diagnos["extension"]["socialstyrelsen"]["categories"]
    assert extras["01"] == {"kod": "A00-B99", "typ": "G"}
    assert extras["U07"] == {"kod": "U07", "grupp": "283"}  # unresolved parent kept
    assert "grupp" not in extras["A00"]


def test_english_documents_omit_missing_labels_without_fallback():
    document = dataset("dodsorsaker", "en", "Cause of Death, annual data")

    validate_metadata(document, adapter="sdb")
    assert document["extension"]["nordicintel"]["language"] == "en"
    assert "label" not in document["dimension"]["diagnos"]
    assert "Spec1_1" not in document["dimension"]["diagnos"]["category"]["label"]
    assert document["dimension"]["region"]["category"]["label"]["0"] == "Sweden"
    assert document["dimension"]["kon"]["label"] == "Sex"


def test_drg_group_and_hospital_shapes():
    document = dataset("drgstatistikislutenvard")

    validate_metadata(document, adapter="sdb")
    grupp = document["dimension"]["grupp"]
    assert grupp["category"]["label"]["1"].startswith("Samtliga huvudgrupper")
    assert grupp["category"]["label"]["2"] == "Sjukdomar i nervsystemet"
    assert grupp["category"]["child"] == {"2": ["3", "4"]}
    assert grupp["extension"]["socialstyrelsen"]["categories"]["3"]["drgkod"] == "A03A"
    region = document["dimension"]["region"]
    assert region["category"]["label"] == {
        "1": "Riket",
        "1565": "10009",
        "2": "S:t Görans sjukhus",
    }
    assert region["category"]["child"] == {"1": ["1565", "2"]}


def test_group_numbers_without_typ_are_not_a_hierarchy():
    variable = {"namn": "indikator", "text": "Indikator", "info": ""}
    categories = [
        {"id": 1, "text": "Besökt tandvården senaste året", "grupp": "1"},
        {"id": 2, "text": "Besökt tandvården senaste två åren", "grupp": "1"},
    ]

    dimension = build_dimension(variable, categories)

    assert "child" not in dimension["category"]
    extras = dimension["extension"]["socialstyrelsen"]["categories"]
    assert extras == {"1": {"grupp": "1"}, "2": {"grupp": "1"}}


def test_multi_year_periods_are_not_annual():
    variables = [{"namn": "ar", "text": "År", "info": ""}]
    periods = [
        {"id": "1988-1990", "text": "1988-1990"},
        {"id": "1987-1989", "text": "1987-1989"},
    ]

    document = build_dataset(
        Subject("skador", "Skador, årsuppgifter", "annual"),
        "sv",
        variables,
        {"ar": periods},
    )

    nordicintel = document["extension"]["nordicintel"]
    assert nordicintel["time_unit"] == "other"
    assert (nordicintel["first_period"], nordicintel["last_period"]) == (
        "1987-1989",
        "1988-1990",
    )


def test_source_record_and_result_dimensions_are_kept():
    row = {"namn": "amning", "text": "Amning, årsuppgifter"}
    document = build_dataset(
        Subject("amning", row["text"], "annual"),
        "sv",
        SDB_DATA["variables"]["sv"]["amning"],
        SDB_DATA["categories"]["sv"]["amning"],
        result_dimensions={"variabel": "grupp"},
        source_record=row,
    )
    validate_metadata(document, adapter="sdb")
    assert document["extension"]["socialstyrelsen"] == {
        "result_dimensions": {"variabel": "grupp"},
        "source_record": row,
    }


@pytest.mark.parametrize(
    ("label", "unit"),
    [
        ("Amning, årsuppgifter", "annual"),
        ("Dödsorsaker, månadsuppgifter", "monthly"),
        ("Något annat", None),
    ],
)
def test_time_unit_from_swedish_label(label, unit):
    assert time_unit_from_swedish_label(label) == unit


def test_html_to_text():
    html = "<h2>Mått</h2><p><b>Antal</b><br />Avser &amp; gäller\r\n  hela året.</p>"

    assert html_to_text(html) == "Mått\nAntal\nAvser & gäller hela året."
    assert html_to_text("  ") is None

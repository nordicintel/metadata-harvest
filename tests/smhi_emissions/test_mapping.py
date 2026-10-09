"""Mapping of the recorded tree and probes to Dataset documents."""

import copy

import pytest

from metadata_harvest.adapters.smhi_emissions.mapping import build_dataset
from metadata_harvest.adapters.smhi_emissions.regions import REGIONS
from metadata_harvest.adapters.smhi_emissions.retrieval import REQUIRED_METADATA
from metadata_harvest.adapters.smhi_emissions.source import (
    parse_filter_options,
    parse_probe,
)
from metadata_harvest.validation import validate_metadata
from tests.smhi_emissions.conftest import (
    FILTER_OPTIONS,
    NATIONAL_CO2,
    NATIONAL_DIOXIN,
    load,
    synthetic_probe,
)


def document(code="CO2", body=None):
    options = parse_filter_options(FILTER_OPTIONS)
    substance = options.substances[code]
    probe = parse_probe(body or synthetic_probe(substance.long_name), options, code)
    result = build_dataset(substance, options, probe)
    validate_metadata(result, adapter="smhi_emissions")
    return result


def test_tree_has_nine_sectors_and_29_substances():
    options = parse_filter_options(FILTER_OPTIONS)
    assert [s.code for s in options.sectors] == [str(n) for n in range(1, 10)]
    assert len(options.substances) == 29 and options.complete
    assert sum(len(s.subsectors) for s in options.sectors) == 52
    # Sectors 1 and 3 reuse their code for their single subsector.
    assert [s.code for s in options.sectors if not s.subsectors] == ["1", "3"]
    co2 = options.substances["CO2"]
    assert (co2.substance_id, co2.long_name) == (11, "Koldioxid (CO2)")
    assert "4.1" in co2.listed_in and "9.1" not in co2.listed_in


def test_identity_label_and_urls():
    result = document("CO2", NATIONAL_CO2)
    shared = result["extension"]["nordicintel"]
    assert shared["provider_code"] == "smhi"
    assert shared["dataset_code"] == "emissionsdatabasen_CO2"
    assert shared["language"] == "sv"
    assert result["label"] == (
        "Koldioxid (CO2): utsläpp till luft efter region, sektor och år"
    )
    assert result["source"] == "SMHI, Nationella emissionsdatabasen"
    assert shared["metadata_url"] == (
        "https://nationellaemissionsdatabasen.smhi.se/api/getfilteroptions/"
    )
    assert shared["data_url"] == (
        "https://nationellaemissionsdatabasen.smhi.se/api/getdata/"
    )
    assert shared["source_url"] == "https://nationellaemissionsdatabasen.smhi.se/"
    assert shared["contacts"][0]["email"] == "emissionsdatabasen@smhi.se"
    assert "updated" not in result  # a submission year is not a date
    assert result["value"] == [] and "status" not in result


def test_dimensions_and_sizes():
    result = document("CO2", NATIONAL_CO2)
    assert result["id"] == ["Region", "Sektor", "Tid", "ContentsCode"]
    assert result["size"] == [312, 62, 10, 1]
    assert result["role"] == {
        "time": ["Tid"],
        "geo": ["Region"],
        "metric": ["ContentsCode"],
    }


def test_region_hierarchy_uses_scb_codes():
    region = document()["dimension"]["Region"]
    category = region["category"]
    assert len(REGIONS) == 312
    assert category["index"][:3] == ["00", "01", "0114"]
    assert category["label"]["00"] == "Sverige"
    assert category["label"]["0180"] == "Stockholm"
    assert len(category["child"]["00"]) == 21
    assert "0180" in category["child"]["01"]
    assert "2584" in category["child"]["25"]
    assert sum(len(kids) for k, kids in category["child"].items() if k != "00") == 290
    assert region["extension"]["nordicintel"] == {
        "elimination": True,
        "elimination_value": "00",
    }


def test_sector_hierarchy_keeps_source_labels():
    sector = document()["dimension"]["Sektor"]
    category = sector["category"]
    assert category["index"][:7] == ["0", "1", "2", "2.3", "2.5", "2.7", "3"]
    assert category["label"]["0"] == "Totalt"
    assert category["child"]["0"] == [str(n) for n in range(1, 10)]
    assert category["child"]["4"][0] == "4.1"
    assert "1" not in category["child"] and "3" not in category["child"]
    assert category["label"]["4.6"] == "Slitage fran däck och bromsar"  # verbatim
    assert sector["extension"]["nordicintel"]["elimination_value"] == "0"


def test_years_come_from_the_probe():
    result = document("Dioxin", NATIONAL_DIOXIN)
    years = result["dimension"]["Tid"]["category"]["index"]
    assert years == [
        "1990",
        "2000",
        "2005",
        "2010",
        "2015",
        *map(str, range(2020, 2025)),
    ]
    shared = result["extension"]["nordicintel"]
    assert (shared["first_period"], shared["last_period"]) == ("1990", "2024")
    assert shared["time_unit"] == "annual"
    assert any("1990, 2000, 2005" in note for note in result["note"])


def test_unit_zero_note_and_co2_equivalents():
    co2 = document("CO2", NATIONAL_CO2)
    contents = co2["dimension"]["ContentsCode"]["category"]
    assert contents["unit"] == {"CO2": {"label": "ton/år"}}
    assert "note" not in contents
    assert any("0" in note and "markeringar" in note for note in co2["note"])
    methane = document("CH4")["dimension"]["ContentsCode"]["category"]
    assert methane["label"] == {"CH4": "Metan (CH4), CO2-ekv."}
    assert "koldioxidekvivalenter" in methane["note"]["CH4"][0]


def test_native_extension_carries_retrieval_metadata():
    result = document("CO2", NATIONAL_CO2)
    native = result["extension"]["smhi_emissions"]
    assert REQUIRED_METADATA == ("/id", "/extension/smhi_emissions")
    assert native["substance"] == {
        "code": "CO2",
        "substance_id": 11,
        "long_name": "Koldioxid (CO2)",
    }
    assert native["submission"] == 2026
    transport = next(s for s in native["sectors"] if s["code"] == "4")
    assert transport["label"] == "Transporter"
    assert transport["subsectors"][0] == {"code": "4.1", "label": "Personbilar"}
    assert native["source_record"]["primary_submission"] == 2026


def test_documents_are_deterministic():
    assert document("CO2", NATIONAL_CO2) == document("CO2", copy.deepcopy(NATIONAL_CO2))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: b["data"]["Koldioxid (CO2)"].update(Rymdfart=[]), "Rymdfart"),
        (lambda b: b.update(data={}), "names no substance"),
        (lambda b: b["data"]["Koldioxid (CO2)"].clear(), "no years"),
        (
            lambda b: b["data"]["Koldioxid (CO2)"]["Totalt"].append(
                {"year": "2025", "value": "n/a"}
            ),
            "unexpected point",
        ),
        (lambda b: b.update(primary_submission="2026"), "primary_submission"),
    ],
    ids=["unknown sector", "no substance", "no years", "text value", "submission"],
)
def test_probe_problems_are_metadata_errors(change, message):
    """Synthetic changes to the recorded CO2 probe."""
    body = copy.deepcopy(NATIONAL_CO2)
    change(body)
    options = parse_filter_options(FILTER_OPTIONS)
    with pytest.raises(ValueError, match=message):
        parse_probe(body, options, "CO2")


def test_capitalisation_drift_is_not_an_unknown_label():
    """Recorded: submission 2025 spells a subsector in lower case."""
    body = load("getdata-stockholm-NOx-transport-sub2025.json")
    label = "inrikes civil sjöfart (inkl privata fritidsbåtar)"
    assert label in body["data"]["Kväveoxider (NOx)"]
    options = parse_filter_options(FILTER_OPTIONS)
    sector = next(s for s in options.sectors if s.code == "4")
    assert label not in {sub.label for sub in sector.subsectors}
    assert label.casefold() in {sub.label.casefold() for sub in sector.subsectors}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.clear(), "unexpected getfilteroptions"),
        (lambda t: t[0].pop("code"), "lacks a numeric code"),
        (lambda t: t[3]["subsectors"][1].update(code="4.1"), "duplicate sector"),
        (
            lambda t: t[3]["subsectors"][1].update(subsector="Personbilar"),
            "ambiguous",
        ),
        (lambda t: t.append(dict(t[0])), "duplicate sector code"),
    ],
    ids=["empty", "no code", "duplicate subsector", "duplicate label", "dup sector"],
)
def test_broken_tree_is_a_whole_scope_failure(change, message):
    tree = copy.deepcopy(FILTER_OPTIONS)
    change(tree)
    with pytest.raises(ValueError, match=message):
        parse_filter_options(tree)

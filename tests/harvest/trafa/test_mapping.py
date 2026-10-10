"""Mapping of recorded Trafikanalys products to Dataset documents."""

import pytest

from apps.harvest.metadata_adapters.trafa.mapping import (
    MeasureProbe,
    build_dataset,
    parse_probe,
    probe_query,
    untranslated_items,
)
from apps.harvest.metadata_adapters.trafa.source import (
    parse_combinations,
    parse_product,
    select_combinations,
)
from apps.harvest.validation import validate_metadata


def product(recorded, name, language="sv"):
    return parse_product(recorded.structure(name, language), name)


def combinations(recorded, name):
    swedish = product(recorded, name)
    listed = parse_combinations(recorded.documentation(swedish.unique_id))
    return {c.dataset_code: c for c in select_combinations(swedish, listed).datasets}


def document(recorded, name, code):
    swedish = product(recorded, name)
    combination = combinations(recorded, name)[code]
    probe = parse_probe(recorded.data(probe_query(swedish, combination)))
    return build_dataset(combination, swedish, "sv", probe)


def test_documentation_page_lists_api_combinations(recorded):
    listed = parse_combinations(recorded.documentation("T10011"))

    assert len(listed) == 10
    assert listed[0] == "T10011|avregunder|avstslut|itrfslut|nyregunder|ar"
    assert "T10011|itrfslut|ar|bussklass|leasing" in listed


def test_page_without_combination_list_is_rejected():
    with pytest.raises(ValueError, match="no API combination list"):
        parse_combinations("<html><body><p>Inget här</p></body></html>")


def test_maximal_combinations_become_datasets_with_sorted_codes(recorded):
    codes = combinations(recorded, "t10011")

    assert sorted(codes) == [
        "T10011|avregunder|ar|avregform",
        "T10011|avregunder|avstslut|itrfslut|nyregunder|ar",
        "T10011|avstslut|itrfslut|agarkat|ar|tillst",
        "T10011|itrfslut|ar|bussklass|leasing",
        "T10011|itrfslut|ar|drivm",
        "T10011|itrfslut|ar|leasing|pass",
        "T10011|nyregunder|ar|dimpo",
    ]
    # Subsets are the same query with a variable eliminated; they are recorded as
    # covered combinations of the maximal one.
    assert codes["T10011|itrfslut|ar|bussklass|leasing"].listed == (
        "T10011|itrfslut|ar|bussklass",
        "T10011|itrfslut|ar|leasing",
        "T10011|itrfslut|ar|bussklass|leasing",
    )


def test_duplicate_and_measureless_combinations(recorded):
    swedish = product(recorded, "t10011")
    listed = [
        "T10011|ar|drivm",
        "T10011|itrfslut|ar|drivm",
        "T10011|drivm|itrfslut|ar",
    ]

    selection = select_combinations(swedish, listed)

    assert selection.without_measures == ["T10011|ar|drivm"]
    assert [c.dataset_code for c in selection.datasets] == ["T10011|itrfslut|ar|drivm"]
    assert selection.datasets[0].listed == tuple(listed[1:])


def test_combination_with_unknown_item_is_rejected(recorded):
    with pytest.raises(ValueError, match="unknown items"):
        select_combinations(product(recorded, "t10011"), ["T10011|itrfslut|nope"])


def test_hierarchy_variables_become_separate_dimensions(recorded):
    result = document(recorded, "t10011", "T10011|avstslut|itrfslut|agarkat|ar|tillst")

    validate_metadata(result, adapter="trafa")
    assert result["id"] == ["ar", "agarkat", "tillst", "measure"]
    assert result["size"] == [25, 3, 3, 2]
    agarkat = result["dimension"]["agarkat"]
    tillst = result["dimension"]["tillst"]
    assert agarkat["label"] == "Ägarkategori"
    assert agarkat["category"]["label"] == {
        "10": "Fysisk person",
        "20": "Juridisk person",
        "t1": "Totalt",
    }
    assert agarkat["extension"] == {
        "nordicintel": {"elimination": True, "elimination_value": "t1"},
        "trafa": {"hierarchy": {"name": "agare", "label": "Ägare"}},
    }
    assert tillst["extension"]["trafa"] == {
        "hierarchy": {"name": "agare", "label": "Ägare"},
        "parent": "agarkat",
    }
    # The H item is not queryable and is not a dimension.
    assert "agare" not in result["dimension"]


def test_several_measures_form_the_metric_dimension(recorded):
    result = document(
        recorded, "t10011", "T10011|avregunder|avstslut|itrfslut|nyregunder|ar"
    )

    validate_metadata(result, adapter="trafa")
    assert result["label"] == (
        "Bussar: Antal i trafik, Antal avställda, Antal nyregistreringar, "
        "Antal avregistreringar efter år"
    )
    assert result["role"] == {"time": ["ar"], "metric": ["measure"]}
    measure = result["dimension"]["measure"]
    assert measure["label"] == "Mått"
    assert measure["category"]["index"] == [
        "itrfslut",
        "avstslut",
        "nyregunder",
        "avregunder",
    ]
    assert measure["category"]["label"]["nyregunder"] == "Antal nyregistreringar"
    assert measure["category"]["note"]["nyregunder"] == ["Avser under perioden"]
    assert measure["extension"] == {"nordicintel": {"elimination": False}}


def test_annual_time_variable(recorded):
    result = document(recorded, "t10011", "T10011|itrfslut|ar|drivm")

    validate_metadata(result, adapter="trafa")
    ar = result["dimension"]["ar"]
    assert ar["label"] == "År"
    assert ar["extension"] == {"nordicintel": {"elimination": False}}
    assert "senaste" not in ar["category"]["index"]  # F items are filters
    nordicintel = result["extension"]["nordicintel"]
    assert nordicintel["time_unit"] == "annual"
    assert (nordicintel["first_period"], nordicintel["last_period"]) == (
        "2001",
        "2025",
    )


def test_monthly_time_variables(recorded):
    result = document(recorded, "t10033", "T10033|avreg|avst|itrf|nyreg|ar|manad")

    validate_metadata(result, adapter="trafa")
    assert result["role"]["time"] == ["ar", "manad"]
    assert result["dimension"]["manad"]["category"]["index"][:2] == ["01", "02"]
    nordicintel = result["extension"]["nordicintel"]
    assert nordicintel["time_unit"] == "monthly"
    assert "first_period" not in nordicintel


def test_units_come_from_the_data_header(recorded):
    swedish = product(recorded, "t04010")
    [combination] = combinations(recorded, "t04010").values()

    query = probe_query(swedish, combination)
    probe = parse_probe(recorded.data(query))
    result = build_dataset(combination, swedish, "sv", probe)

    assert query == "t04010|fordonkm|ar:2025"
    assert probe.units == {"fordonkm": "miljoner km"}
    validate_metadata(result, adapter="trafa")
    assert result["dimension"]["measure"]["category"]["unit"] == {
        "fordonkm": {"label": "miljoner km"}
    }


def test_identity_urls_and_retrieval_metadata(recorded):
    result = document(recorded, "t10011", "T10011|itrfslut|ar|drivm")

    nordicintel = result["extension"]["nordicintel"]
    assert result["value"] == []
    assert result["source"] == "Trafikanalys"
    assert nordicintel["provider_code"] == "trafa"
    assert nordicintel["dataset_code"] == "T10011|itrfslut|ar|drivm"
    assert nordicintel["language"] == "sv"
    assert nordicintel["metadata_url"] == (
        "https://api.trafa.se/api/structure?query=t10011&lang=SV"
    )
    assert nordicintel["data_url"] == "https://api.trafa.se/api/data"
    assert nordicintel["doc_url"] == (
        "https://www.trafa.se/sidor/api-dokumentation/?code=T10011"
    )
    assert result["extension"]["trafa"] == {
        "product": "t10011",
        "product_id": "T10011",
        "measure_dimension": "measure",
        "api_combinations": ["T10011|itrfslut|ar|drivm"],
        "active_from": "2019-01-10T10:17:00",
        "source_record": {
            "product": {"name": "t10011", "unique_id": "T10011", "label": "Bussar"},
            "combination": {"measures": ["itrfslut"], "variables": ["ar", "drivm"]},
        },
    }
    assert "next_release" not in nordicintel  # NextPublishDate is 0001-01-01


def test_probe_errors_and_next_publish_date():
    with pytest.raises(ValueError, match="reported errors"):
        parse_probe({"Errors": ["Felaktig fråga"], "Header": {"Column": []}})
    probe = parse_probe(
        {"Header": {"Column": []}, "NextPublishDate": "2026-10-15T09:30:00"}
    )
    assert probe.next_publish == "2026-10-15T09:30:00"


def test_english_echoes_are_not_translations(recorded):
    swedish = product(recorded, "t10011")
    english = product(recorded, "t10011", "en")
    combination = combinations(recorded, "t10011")["T10011|itrfslut|ar|drivm"]

    missing = untranslated_items(combination, english, swedish)

    # Product label "T10011", measure "Itrfslut", variables "Ar" and "Drivm" all echo
    # API codes.
    assert missing == ["T10011", "itrfslut", "ar", "drivm"]
    with pytest.raises(ValueError, match="no English translation"):
        build_dataset(combination, english, "en", MeasureProbe(), reference=swedish)


def _translate(structure, translations):
    for item in structure["StructureItems"]:
        if item["UniqueId"] in translations:
            item["Label"] = translations[item["UniqueId"]]
        _translate({"StructureItems": item.get("StructureItems") or []}, translations)
    return structure


def test_english_document_keeps_only_real_translations(recorded):
    """English labels here are test inputs: Trafikanalys supplies none today."""
    swedish = product(recorded, "t10011")
    translated = _translate(
        recorded.structure("t10011", "en"),
        {
            "T10011": "Buses",
            "Ditrfslut": "Number in traffic",
            "Dar": "Year",
            "Ddrivm": "Fuel",
            "DdrivmDV101": "Petrol",
        },
    )
    english = parse_product(translated, "t10011")
    combination = combinations(recorded, "t10011")["T10011|itrfslut|ar|drivm"]

    assert untranslated_items(combination, english, swedish) == []
    result = build_dataset(
        combination,
        english,
        "en",
        MeasureProbe(units={"itrfslut": "st"}),
        reference=swedish,
        reference_probe=MeasureProbe(units={"itrfslut": "st"}),
    )

    validate_metadata(result, adapter="trafa")
    assert result["label"] == "Buses: Number in traffic by Year and Fuel"
    assert result["source"] == "Transport Analysis"
    assert result["extension"]["nordicintel"]["language"] == "en"
    assert result["extension"]["nordicintel"]["metadata_url"].endswith("lang=EN")
    drivm = result["dimension"]["drivm"]["category"]
    # "Diesel", "El", … are the Swedish labels echoed; only "Petrol" is translated.
    assert drivm["label"] == {"101": "Petrol"}
    assert len(drivm["index"]) == 10
    measure = result["dimension"]["measure"]["category"]
    assert measure["label"] == {"itrfslut": "Number in traffic"}
    assert "note" not in measure  # Swedish description absent from English
    assert "unit" not in measure  # unit text identical to Swedish
    assert not any("Avser" in note for note in result["note"])

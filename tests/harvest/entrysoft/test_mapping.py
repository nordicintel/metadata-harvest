"""Dataset records become valid, eliminable, queryable Dataset documents."""

from apps.harvest.metadata_adapters.entrysoft.datasets import build_datasets
from apps.harvest.metadata_adapters.entrysoft.mapping import (
    BLANK,
    ROW_COUNT,
    codes,
    time_unit,
    to_document,
)
from apps.harvest.validation import validate_metadata
from tests.harvest.entrysoft.test_datasets import document

PROVIDER = {"code": "x", "label": "Exempel kommun", "base_url": "https://x"}


def dataset(*docs):
    datasets, _ = build_datasets(list(docs))
    for item in datasets:
        item["records"] = [
            d["extension"]["metadata_harvest"]
            for d in docs
            if d["extension"]["metadata_harvest"]["table_url"] in item["tables"]
        ]
        item["cells_unique"] = True
    return datasets


def test_cube_document_is_valid_and_eliminable():
    (item,) = dataset(
        document(
            "x",
            "d1",
            "Fakturor",
            {
                "forvaltning": ["Skola", "Vård"],
                "ar": ["2023", "2024"],
                "kommun_id": ["1480"],
            },
            100,
            measures=("belopp", "antal"),
        )
    )
    doc = to_document(item, PROVIDER)
    validate_metadata(doc, adapter="entrysoft")
    assert doc["id"] == ["forvaltning", "ar", "measure"]
    assert doc["role"] == {"metric": ["measure"], "time": ["ar"]}
    assert doc["source"] == "Exempel kommun"
    for code in ("forvaltning", "ar"):
        assert doc["dimension"][code]["extension"]["nordicintel"] == {
            "elimination": True,
            "elimination_value": None,
        }
    assert "nordicintel" not in doc["dimension"]["measure"]["extension"]
    shared = doc["extension"]["nordicintel"]
    assert shared["time_unit"] == "annual" and (
        shared["first_period"],
        shared["last_period"],
    ) == ("2023", "2024")
    assert shared["data_url"] == "https://x/rowstore/dataset/d1"
    native = doc["extension"]["entrysoft"]
    assert native["constants"] == {"kommun_id": "1480"} and native["tables"] == [
        "https://x/rowstore/dataset/d1"
    ]
    assert native["source_record"][0]["dataset_code"] == "d1"


def test_blank_values_share_one_code_and_wide_columns_become_filters():
    names = [f"Leverantör {i}" for i in range(1200)]
    (item,) = dataset(
        document(
            "x",
            "d",
            "Ledger",
            {"konto": ["10", "", " "], "leverantor": names},
            5000,
            measures=("belopp",),
        )
    )
    doc = to_document(item, PROVIDER)
    validate_metadata(doc, adapter="entrysoft")
    assert list(doc["dimension"]["konto"]["category"]["index"]) == ["10", BLANK]
    assert doc["id"] == ["konto", "measure"]
    assert doc["extension"]["entrysoft"]["filters"]["leverantor"]["enumerable"] is False


def test_dataset_without_measures_counts_rows():
    (item,) = dataset(
        document(
            "x",
            "d",
            "Enkät",
            {"myndighet": [f"M{i}" for i in range(20)], "svar": ["ja", "nej"]},
            40,
        )
    )
    doc = to_document(item, PROVIDER)
    validate_metadata(doc, adapter="entrysoft")
    assert list(doc["dimension"]["measure"]["category"]["index"]) == [ROW_COUNT]
    assert (
        doc["dimension"]["measure"]["extension"]["entrysoft"]["measure_kind"]
        == "row_count"
    )


def test_merged_dataset_has_a_part_dimension_mapped_to_tables():
    docs = [
        document(
            "h",
            f"d{y}",
            f"Reskontra {y}",
            {"forvaltning": ["A", "B"]},
            100,
            distribution="Januari - December",
            measures=("belopp",),
        )
        for y in (2023, 2024)
    ]
    (item,) = dataset(*docs)
    doc = to_document(item, PROVIDER)
    validate_metadata(doc, adapter="entrysoft")
    assert doc["id"][0] == "part"
    assert doc["dimension"]["part"]["extension"]["entrysoft"]["tables"] == {
        "2023": "https://h/rowstore/dataset/d2023",
        "2024": "https://h/rowstore/dataset/d2024",
    }
    assert "part" not in doc["role"].get("time", [])


def test_helpers():
    assert codes(["a", "", " ", "a"]) == ["a", BLANK]
    assert time_unit(["2023", "2024"]) == "annual"
    assert time_unit(["2023-01", "2023-02"]) == "monthly"
    assert time_unit(["2023-01-05", "2023-01-06 10:00"]) == "daily"
    assert time_unit(["HT2023"]) == "other" and time_unit([]) is None

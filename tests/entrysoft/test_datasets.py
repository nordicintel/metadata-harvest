"""Rules that turn harvested tables into datasets, on hand-made documents."""

from harvesting.adapters.entrysoft.datasets import (
    MAX_LISTED,
    apply_column_rules,
    build_datasets,
    stem_and_period,
)


def document(
    provider,
    dataset_code,
    title,
    columns,
    rowcount,
    *,
    distribution="",
    dataset_title=None,
    measures=(),
):
    """A harvested JSON-stat document with ``columns`` = {name: categories}."""
    dimension = {
        name: {
            "label": name,
            "category": {
                "index": {v: i for i, v in enumerate(values)},
                "label": {v: v for v in values},
            },
            "extension": {
                "rowstore": {
                    "column": name,
                    "role": "dimension",
                    "reason": "text values",
                    "time": name == "datum",
                }
            },
        }
        for name, values in columns.items()
    }
    if measures:
        dimension["measure"] = {
            "category": {"index": {m: i for i, m in enumerate(measures)}},
            "extension": {"rowstore": {}},
        }
    return {
        "label": title,
        "source": provider,
        "dimension": dimension,
        "extension": {
            "metadata_harvest": {
                "provider_code": provider,
                "dataset_code": dataset_code,
                "title": title,
                "table_url": f"https://{provider}/rowstore/dataset/{dataset_code}",
                "catalogue": {
                    "dataset_title": {"sv": dataset_title or title},
                    "distribution_title": {"sv": distribution},
                },
            },
            "rowstore": {"rowcount": rowcount, "columns": list(columns)},
        },
    }


def test_period_tokens_are_split_from_the_title_stem():
    assert stem_and_period("Leverantörsreskontra 2024 Januari - September") == (
        "leverantörsreskontra",
        "2024 januari september",
    )
    assert stem_and_period("Riktvärdeangivelser för småhus 2021-2023")[1] == "2021-2023"


def test_constants_identifiers_and_pairs_leave_the_dimensions():
    ids = [str(100000 + i) for i in range(400)]
    doc = document(
        "x",
        "d1",
        "Fakturor",
        {
            "kommun_id": ["1480"],
            "verifikationsnummer": ids,
            "konto_nr": ["10", "20", "30"],
            "konto_text": ["Löner", "Hyra", "El"],
            "forvaltning": ["A", "B"],
        },
        rowcount=400,
        measures=("belopp",),
    )
    view = apply_column_rules(doc)
    assert view.constants == {"kommun_id": "1480"}
    assert view.identifiers == ["verifikationsnummer"]
    assert [(d["column"], d.get("label_column")) for d in view.dimensions] == [
        ("konto_nr", "konto_text"),
        ("forvaltning", None),
    ]


def test_near_unique_names_stay_dimensions_and_lone_identifiers_become_one():
    agencies = {f"Myndighet {i}" for i in range(300)}
    assert apply_column_rules(
        document("x", "d", "Enkät", {"myndighet": sorted(agencies)}, 300)
    ).dimensions
    view = apply_column_rules(
        document(
            "x",
            "d",
            "Hyror",
            {"nr_omrad": [str(i) for i in range(300)]},
            300,
            measures=("1929",),
        )
    )
    assert [d["column"] for d in view.dimensions] == [
        "nr_omrad"
    ] and view.identifiers == []


def test_wide_and_dense_time_dimensions_are_not_listed():
    stamps = [f"2024-01-01 {h:02d}:{m:02d}" for h in range(24) for m in range(60)]
    names = [f"Leverantör {i}" for i in range(MAX_LISTED + 1)]
    view = apply_column_rules(
        document("x", "d", "Luft", {"start": stamps, "leverantor": names}, 5000)
    )
    start, supplier = view.dimensions
    assert (
        start["time"]
        and start["range"] == [stamps[0], stamps[-1]]
        and "categories" not in start
    )
    assert supplier["enumerable"] is False and "categories" not in supplier


def test_yearly_tables_merge_into_one_dataset_with_a_part_dimension():
    docs = [
        document(
            "h",
            f"d{year}",
            f"Leverantörsreskontra {year}",
            {"forvaltning": ["A", "B"] if year < 2024 else ["B", "C"]},
            100,
            distribution="Januari - December",
        )
        for year in (2022, 2023, 2024)
    ] + [
        document(
            "h", "other", "Badplatser", {"forvaltning": ["A"], "namn": ["x", "y"]}, 2
        )
    ]
    datasets, dropped = build_datasets(docs)
    assert dropped == [] and len(datasets) == 2
    merged = next(d for d in datasets if len(d["tables"]) == 3)
    assert merged["title"] == "Leverantörsreskontra"
    part, forvaltning = merged["dimensions"]
    assert part["id"] == "part" and part["categories"] == ["2022", "2023", "2024"]
    assert part["tables"]["2023"].endswith("/d2023") and forvaltning["categories"] == [
        "A",
        "B",
        "C",
    ]


def test_tables_of_one_catalogue_dataset_merge_by_distribution_title():
    docs = [
        document(
            "t",
            f"c{i}",
            "Trängselskatt",
            {"time": ["06:00", "07:00"]},
            10,
            dataset_title="Trängselskatt tariffer",
            distribution=city,
        )
        for i, city in enumerate(("Stockholm", "Göteborg"))
    ]
    (dataset,), _ = build_datasets(docs)
    assert dataset["dimensions"][0]["categories"] == ["Stockholm", "Göteborg"]


def test_test_and_single_row_tables_are_dropped():
    docs = [
        document("x", "a", "Reskontra Marcus Test", {"k": ["1", "2"]}, 10),
        document("x", "b", "Lediga tomter", {"k": ["1"]}, 1),
    ]
    datasets, dropped = build_datasets(docs)
    assert datasets == [] and [d["reason"] for d in dropped] == [
        "test table",
        "single row",
    ]

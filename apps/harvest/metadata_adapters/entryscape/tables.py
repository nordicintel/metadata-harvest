"""Build the per-table structure document the dataset rules read."""

from __future__ import annotations

from copy import deepcopy

from apps.harvest.metadata_adapters.entryscape.categories import clean_name

MEASURE_DIMENSION = "measure"


def table_document(record: dict, structure: dict) -> dict:
    """Map an inferred table structure to metadata-only JSON-stat2.

    Dimension columns become dimensions with every category; measure columns
    become categories of a synthetic ``measure`` dimension with role ``metric``.
    """
    ids, sizes, dimensions = [], [], {}
    time_ids = []
    for column in structure["dimensions"]:
        dimension_id = clean_name(column) or column
        while dimension_id in dimensions:
            dimension_id += "_"
        categories = structure["categories"][column]
        ids.append(dimension_id)
        sizes.append(len(categories))
        dimensions[dimension_id] = {
            "label": dimension_id,
            "category": {
                "index": {value: position for position, value in enumerate(categories)},
                "label": {value: value for value in categories},
            },
            "extension": {"rowstore": {"column": column, **structure["roles"][column]}},
        }
        if structure["roles"][column].get("time"):
            time_ids.append(dimension_id)
    role = {}
    if structure["measures"]:
        measures = [clean_name(column) or column for column in structure["measures"]]
        ids.append(MEASURE_DIMENSION)
        sizes.append(len(measures))
        dimensions[MEASURE_DIMENSION] = {
            "label": MEASURE_DIMENSION,
            "category": {
                "index": {name: position for position, name in enumerate(measures)},
                "label": {name: name for name in measures},
            },
            "extension": {
                "rowstore": {
                    "columns": dict(zip(measures, structure["measures"], strict=True)),
                    "reasons": {
                        clean_name(c) or c: structure["roles"][c]["reason"]
                        for c in structure["measures"]
                    },
                }
            },
        }
        role["metric"] = [MEASURE_DIMENSION]
    if time_ids:
        role["time"] = time_ids
    document = {
        "version": "2.0",
        "class": "dataset",
        "label": record["title"] or record["dataset_code"],
        "updated": record["published"],
        "source": record["provider_code"],
        "id": ids,
        "size": sizes,
        "dimension": dimensions,
        "value": [],
        "extension": {
            "metadata_harvest": deepcopy(record),
            "rowstore": {
                key: structure[key]
                for key in (
                    "rowcount",
                    "columns",
                    "strategy",
                    "requests",
                    "full_scan_requests",
                    "dimension_key_unique_in_sample",
                )
            },
        },
    }
    if role:
        document["role"] = role
    return document

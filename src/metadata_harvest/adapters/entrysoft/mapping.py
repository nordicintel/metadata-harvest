"""Map a grouped RowStore dataset record to the shared Dataset document.

Listed dimensions and the synthetic ``measure`` dimension form the cube. Wide
and dense-time columns have no finite category list, so they are not dimensions;
they stay queryable through the adapter and are recorded under
``extension.entrysoft.filters``. Every column dimension is eliminable: an omitted
dimension is aggregated by retrieval, like cells that cover several rows.
"""

import re
from copy import deepcopy

from metadata_harvest.adapters.entrysoft.datasets import MAPPING_VERSION, PART_DIMENSION
from metadata_harvest.validation import validate_metadata

ADAPTER = "entrysoft"
LANGUAGE = "sv"
MEASURE_DIMENSION = "measure"
BLANK = "(tom)"
"""Category code standing for a blank cell value; codes cannot be blank."""
ROW_COUNT = "rows"
"""Measure category of datasets without numeric measures: the number of rows."""
STATUS_NOTE = (
    "Retrieval sums numeric measures over the rows behind a cell and reports the "
    "number of rows as `rows=N` in status when it exceeds one; an omitted dimension "
    "is aggregated the same way. A non-numeric value is null with its text in status."
)
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_MONTH = re.compile(r"^(?:19|20)\d{2}-(?:0[1-9]|1[0-2])$")
_DATE = re.compile(r"^(?:19|20)\d{2}-\d{2}-\d{2}")
_QUARTER = re.compile(r"^(?:19|20)\d{2}[-\s]?[QqKk][1-4]$")


def codes(values: list[str]) -> list[str]:
    """Category codes for raw cell values: blank values share one code."""
    result: list[str] = []
    for value in values:
        code = BLANK if not str(value).strip() else str(value)
        if code not in result:
            result.append(code)
    return result


def _category(values: list[str]) -> dict:
    return {
        "index": {value: position for position, value in enumerate(values)},
        "label": {value: value for value in values},
    }


def source_date(value) -> str | None:
    match = re.match(r"^((?:19|20)\d{2}-\d{2}-\d{2})", str(value or ""))
    return match[1] if match else None


def time_unit(codes: list[str]) -> str | None:
    """Period granularity read from a time dimension's codes."""
    sample = [code.strip() for code in codes[:200] if code.strip()]
    if not sample:
        return None
    for pattern, unit in (
        (_YEAR, "annual"),
        (_MONTH, "monthly"),
        (_QUARTER, "quarterly"),
        (_DATE, "daily"),
    ):
        if all(pattern.match(code) for code in sample):
            return unit
    return "other"


def _description(catalogue: dict) -> str | None:
    texts = catalogue.get("dataset_description") or {}
    text = texts.get("sv") or texts.get("en") or next(iter(texts.values()), None)
    return " ".join(str(text).split()) or None if text else None


def _metadata_url(record: dict) -> str | None:
    for path in record.get("paths") or []:
        distribution = path.get("distribution") or ""
        if "/resource/" in distribution:
            return distribution.replace("/resource/", "/metadata/", 1)
    return None


def to_document(dataset: dict, provider: dict) -> dict:
    """The validated metadata-only Dataset document for one dataset record."""
    ids, sizes, dimensions, filters, time_ids = [], [], {}, {}, []
    for spec in dataset["dimensions"]:
        native = {k: v for k, v in spec.items() if k not in ("id", "categories")}
        if "categories" not in spec:
            filters[spec["id"]] = native
            continue
        categories = codes(spec["categories"])
        ids.append(spec["id"])
        sizes.append(len(categories))
        dimensions[spec["id"]] = {
            "label": spec["id"],
            "category": _category(categories),
            "extension": {
                "nordicintel": {"elimination": True, "elimination_value": None},
                ADAPTER: native,
            },
        }
        if spec.get("time") and spec["id"] != PART_DIMENSION:
            time_ids.append(spec["id"])
    measures = list(dataset["measures"]) or [ROW_COUNT]
    ids.append(MEASURE_DIMENSION)
    sizes.append(len(measures))
    dimensions[MEASURE_DIMENSION] = {
        "label": "Mått" if dataset["measures"] else "Antal rader",
        "category": _category(measures),
        "extension": {
            ADAPTER: {"measure_kind": "columns" if dataset["measures"] else "row_count"}
        },
    }
    role = {"metric": [MEASURE_DIMENSION]}
    if time_ids:
        role["time"] = time_ids

    catalogue = dataset["catalogue"]
    records = dataset.get("records") or []
    first = records[0] if records else {}
    shared = {
        "provider_code": dataset["provider_code"],
        "dataset_code": dataset["dataset_code"],
        "language": LANGUAGE,
        "data_url": dataset["tables"][0],
        "doc_url": dataset["tables"][0] + "/swagger",
    }
    if metadata_url := _metadata_url(first):
        shared["metadata_url"] = metadata_url
    if description := _description(catalogue):
        shared["description"] = description
    if time_ids and (
        unit := time_unit(list(dimensions[time_ids[0]]["category"]["index"]))
    ):
        shared["time_unit"] = unit
        periods = list(dimensions[time_ids[0]]["category"]["index"])
        shared["first_period"], shared["last_period"] = min(periods), max(periods)

    native = {
        "kind": dataset["kind"],
        "tables": list(dataset["tables"]),
        "rowcount": dataset["rowcount"],
        "filters": filters,
        "identifiers": list(dataset["identifiers"]),
        "constants": deepcopy(dataset["constants"]),
        "cells_unique": dataset.get("cells_unique"),
        "mapping_version": MAPPING_VERSION,
        "source_record": deepcopy(records),
    }
    updated = dataset.get("updated") or catalogue.get("dataset_modified")
    if updated:
        native["updated_original"] = updated

    notes = [STATUS_NOTE]
    document = {
        "version": "2.0",
        "class": "dataset",
        "label": dataset["title"] or dataset["dataset_code"],
        "source": provider.get("label") or dataset["provider_code"],
        "note": notes,
        "id": ids,
        "size": sizes,
        "role": role,
        "dimension": dimensions,
        "value": [],
        "extension": {"nordicintel": shared, ADAPTER: native},
    }
    if date := source_date(updated):
        document["updated"] = date
    validate_metadata(document, adapter=ADAPTER)
    return document

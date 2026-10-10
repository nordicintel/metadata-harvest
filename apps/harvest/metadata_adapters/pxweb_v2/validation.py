"""Validate native source metadata without remapping or reducing it."""

from datetime import datetime

from apps.harvest.metadata_adapters.pxweb_v1.mapping import source_record
from apps.harvest.validation import validate_metadata as validate_shared


def text(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid {name}")
    return value


def validate_table(table: dict) -> None:
    for field in ("id", "label", "updated"):
        text(table.get(field), f"table {field}")
    datetime.fromisoformat(table["updated"])
    for field in ("paths", "links"):
        if not isinstance(table.get(field), list):
            raise ValueError(f"Invalid table {field}")


def codelist_ids(dataset: dict) -> set[str]:
    ids = set()
    for dimension in dataset["dimension"].values():
        extension = dimension.get("extension", {})
        entries = extension.get("pxweb_v2", extension).get("codelists", [])
        if not isinstance(entries, list):
            raise ValueError("Invalid codelist references")
        for entry in entries:
            ids.add(text(entry.get("id"), "codelist reference"))
    return ids


def validate_metadata(dataset: dict, record: dict) -> None:
    if "nordicintel" in dataset.get("extension", {}):
        validate_shared(dataset, adapter="pxweb_v2")
        saved = source_record(dataset, "pxweb_v2")
        expected = {
            key: record[key] for key in ("provider_code", "dataset_code", "language")
        }
        actual = dataset["extension"]["nordicintel"]
        if (
            any(
                actual.get(key) != value or saved.get(key) != value
                for key, value in expected.items()
            )
            or saved["catalogue"]["updated"] != record["catalogue"]["updated"]
        ):
            raise ValueError("Metadata identity or source timestamp mismatch")
        codelist_ids(dataset)
        return
    if (
        dataset.get("class") != "dataset"
        or dataset.get("version") != "2.0"
        or dataset.get("value") != []
    ):
        raise ValueError("Expected metadata-only JSON-stat2 with value: []")
    px = dataset.get("extension", {}).get("px", {})
    if (
        px.get("tableid") != record["dataset_code"]
        or px.get("language") != record["language"]
    ):
        raise ValueError("Metadata table or language mismatch")
    text(dataset.get("label"), "metadata title")
    if dataset.get("updated") != record["catalogue"]["updated"]:
        raise ValueError("Metadata and catalogue source timestamps differ")
    ids, sizes, dimensions = (
        dataset.get("id"),
        dataset.get("size"),
        dataset.get("dimension"),
    )
    if (
        not isinstance(ids, list)
        or not ids
        or len(set(ids)) != len(ids)
        or not isinstance(sizes, list)
        or len(ids) != len(sizes)
        or not isinstance(dimensions, dict)
        or set(ids) != set(dimensions)
    ):
        raise ValueError("Invalid metadata dimensions")
    for code, size in zip(ids, sizes, strict=True):
        if type(size) is not int or size <= 0:
            raise ValueError("Invalid dimension size")
        category = dimensions[code]["category"]
        index, labels = category.get("index"), category.get("label")
        if isinstance(index, list):
            keys = index
        elif (
            isinstance(index, dict)
            and all(type(v) is int for v in index.values())
            and sorted(index.values()) == list(range(size))
        ):
            keys = list(index)
        else:
            raise ValueError("Invalid category index")
        if (
            len(keys) != size
            or len(set(keys)) != size
            or not isinstance(labels, dict)
            or set(labels) != set(keys)
        ):
            raise ValueError("Incomplete category labels or counts")
    codelist_ids(dataset)


def validate_codelist(payload: dict, code: str, language: str) -> None:
    if payload.get("id") != code or payload.get("language") != language:
        raise ValueError("Codelist identity or language mismatch")
    text(payload.get("label"), "codelist label")
    if payload.get("type") not in {"Valueset", "Aggregation"} or not isinstance(
        payload.get("values"), list
    ):
        raise ValueError("Invalid codelist type or values")
    seen = set()
    for value in payload["values"]:
        key = text(value.get("code"), "codelist value code")
        text(value.get("label"), "codelist value label")
        mapping = value.get("valueMap")
        if (
            key in seen
            or not isinstance(mapping, list)
            or not all(isinstance(v, str) and v for v in mapping)
        ):
            raise ValueError("Duplicate codelist value or invalid value map")
        seen.add(key)

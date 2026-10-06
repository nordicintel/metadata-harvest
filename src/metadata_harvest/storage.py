"""Atomic JSON files and stable dataset identities for the harvesting workflow."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from metadata_harvest.metadata import _categories


def now():
    return datetime.now(UTC).isoformat()


def identity(record):
    values = (
        record["provider_code"],
        record.get("dataset_code", record.get("id")),
        record["language"],
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(
            "Dataset identity requires provider_code, dataset_code and language"
        )
    return values


def filename(record):
    key = identity(record)
    digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[
        :16
    ]
    readable = "__".join(quote(value, safe="")[:65] for value in key)
    return f"{readable}__{digest}.json"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def timestamp(value):
    """Normalize only the comparison key; never change the provider's stored date."""
    parsed = datetime.fromisoformat(value if len(value) > 7 else value + "-01")
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def location(record):
    return (record["database_id"], record["path"])


def group_records(records):
    """One logical dataset with all locations, using the newest location as primary."""
    groups = {}
    for record in records:
        groups.setdefault(identity(record), {})[location(record)] = record
    result = []
    for key, locations in sorted(groups.items()):
        primary = min(
            locations.values(),
            key=lambda row: (-timestamp(row["published"]).timestamp(), row["api_url"]),
        )
        grouped = {**primary, "dataset_code": key[1]}
        grouped["paths"] = [
            {
                field: value
                for field, value in row.items()
                if field
                not in {"paths", "provider_code", "dataset_code", "language", "id"}
            }
            for _, row in sorted(locations.items())
        ]
        result.append(grouped)
    return result


def validate_dataset(dataset, record):
    if (
        not isinstance(dataset, dict)
        or dataset.get("class") != "dataset"
        or dataset.get("version") != "2.0"
        or dataset.get("value") != []
    ):
        raise ValueError("Expected a metadata-only JSON-stat2 dataset")
    saved = dataset.get("extension", {}).get("metadata_harvest", {})
    if (
        identity(saved) != identity(record)
        or dataset.get("updated") != record["published"]
    ):
        raise ValueError("Metadata identity or successful timestamp mismatch")
    if saved.get("paths") != record["paths"]:
        raise ValueError("Metadata has lost table locations")
    ids, sizes = dataset.get("id"), dataset.get("size")
    if (
        not isinstance(ids, list)
        or not ids
        or not isinstance(sizes, list)
        or len(ids) != len(sizes)
    ):
        raise ValueError("Invalid completed metadata dimensions")
    for code, size in zip(ids, sizes, strict=True):
        _categories(dataset["dimension"][code], size)


def refresh_identity(dataset, record):
    """Update listing metadata without replacing the last successful build date."""
    dataset["label"] = record["title"]
    saved = dataset["extension"]["metadata_harvest"]
    saved.update(record)
    saved["published"] = dataset["updated"]
    return dataset

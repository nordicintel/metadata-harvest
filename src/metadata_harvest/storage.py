"""Atomic JSON files and stable dataset identities for the harvesting workflow."""

import hashlib
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from metadata_harvest.adapters.pxweb_v1.metadata import _categories


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


def document_identity(document: dict) -> dict:
    """Return the identity record from a listing or a completed dataset."""
    if document.get("class") == "dataset":
        return document["extension"]["metadata_harvest"]
    return document


def collection_filename(document: dict) -> str:
    """One collision-safe JSONL filename per provider and language."""
    provider, _, language = identity(document_identity(document))
    digest = hashlib.sha256(json.dumps([provider, language]).encode()).hexdigest()[:12]
    return f"{quote(provider, safe='')[:65]}__{quote(language, safe='')[:65]}__{digest}.jsonl"


class JsonlStore:
    """Latest documents grouped by provider/language, keyed by stable task ID.

    Legacy per-dataset JSON is read for migration and unfinished-run recovery.
    Save stages every group before replacing files, verifies the written content,
    and only then removes superseded files. Collectors publish all changes in one
    Git commit; filesystem atomicity is per group, not across the collection.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.records: dict[str, dict] = {}
        self._legacy: dict[Path, dict] = {}
        for path in sorted(self.directory.glob("*.jsonl")):
            with path.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    document = json.loads(line)
                    name = filename(document_identity(document))
                    if collection_filename(document) != path.name:
                        raise ValueError(f"Wrong provider/language in {path}:{number}")
                    if name in self.records:
                        raise ValueError(f"Duplicate dataset in {path}:{number}")
                    self.records[name] = document
        for path in sorted(self.directory.glob("*.json")):
            document = read(path)
            name = filename(document_identity(document))
            if path.name != name:
                raise ValueError(f"Legacy dataset filename mismatch: {path.name}")
            if name in self.records and self.records[name] != document:
                raise ValueError(f"Conflicting legacy and JSONL dataset: {name}")
            self.records[name] = document
            self._legacy[path] = deepcopy(document)

    def save(self) -> None:
        """Write deterministic JSONL; unchanged groups retain their exact bytes."""
        groups: dict[str, list[dict]] = {}
        for name, document in sorted(self.records.items()):
            if filename(document_identity(document)) != name:
                raise ValueError(f"Dataset key does not match identity: {name}")
            groups.setdefault(collection_filename(document), []).append(document)
        self.directory.mkdir(parents=True, exist_ok=True)
        staged: dict[Path, Path] = {}
        try:
            for name, documents in groups.items():
                path = self.directory / name
                temporary = self.directory / f".{uuid4().hex}.tmp"
                staged[path] = temporary
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    for document in documents:
                        stream.write(
                            json.dumps(
                                document,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                                allow_nan=False,
                            )
                            + "\n"
                        )
                    stream.flush()
                    os.fsync(stream.fileno())
                with temporary.open(encoding="utf-8") as stream:
                    if [json.loads(line) for line in stream] != documents:
                        raise ValueError("JSONL serialization changed document content")
            # Detect edits to legacy input before replacing or removing anything.
            for path, original in self._legacy.items():
                if read(path) != original:
                    raise ValueError(f"Legacy input changed during migration: {path}")
            for path, temporary in staged.items():
                if not path.exists() or path.read_bytes() != temporary.read_bytes():
                    temporary.replace(path)
            for name, documents in groups.items():
                with (self.directory / name).open(encoding="utf-8") as stream:
                    if [json.loads(line) for line in stream] != documents:
                        raise ValueError("Written JSONL failed content verification")
            for path in self._legacy:
                path.unlink()
            self._legacy.clear()
            for path in self.directory.glob("*.jsonl"):
                if path.name not in groups:
                    path.unlink()
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)


def migrate_storage(data: str | Path) -> dict[str, int]:
    """Losslessly compact existing corpus files without contacting providers."""
    counts = {}
    for folder in ("table-records", "metadata"):
        store = JsonlStore(Path(data) / folder)
        original = store.records
        store.save()
        if JsonlStore(store.directory).records != original:
            raise ValueError(f"Migration verification failed: {folder}")
        counts[folder] = len(original)
    return counts

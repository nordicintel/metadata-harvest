"""Atomic JSON files and stable dataset identities for the harvesting workflow."""

import gzip
import hashlib
import json
import os
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from harvesting.accepted import contribution_documents, write_metadata
from harvesting.mapping import source_record, v1_listing_fields
from harvesting.models import PROFILE_VERSION, Dataset
from harvesting.validation import validate_metadata


def now():
    return datetime.now(UTC).isoformat()


def write_jsonl(path, documents):
    """Atomically serialize trusted records; no schema or corpus verification pass."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as raw:
            with (
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
                if path.suffix == ".gz"
                else nullcontext(raw)
            ) as stream:
                for document in documents:
                    stream.write(
                        (
                            json.dumps(
                                document,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
            raw.flush()
            os.fsync(raw.fileno())
        if not path.exists() or path.read_bytes() != temporary.read_bytes():
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        Path(path).name == "result.json"
        and value.get("profile_version") != PROFILE_VERSION
    ):
        raise ValueError("Incompatible legacy worker artifact; run the current worker")
    return value


def write(path, value):
    path = Path(path)
    if path.name == "result.json":
        value["profile_version"] = PROFILE_VERSION
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
    validate_metadata(dataset, adapter="pxweb_v1")
    saved = source_record(dataset, "pxweb_v1")
    if (
        identity(saved) != identity(record)
        or saved.get("published") != record["published"]
    ):
        raise ValueError("Metadata identity or accepted source timestamp mismatch")
    shared = dataset["extension"]["nordicintel"]
    expected = v1_listing_fields(record)
    if any(
        shared.get(k) != expected.get(k)
        for k in ("data_url", "metadata_url", "source_url", "paths")
    ):
        raise ValueError("Canonical routes or paths disagree with source listing")
    if saved.get("paths") != record.get("paths"):
        raise ValueError("Metadata has lost table locations")


def refresh_identity(dataset, record):
    """Refresh listing context without changing accepted metadata title/date."""
    from copy import deepcopy

    saved = source_record(dataset, "pxweb_v1")
    published = saved["published"]
    saved.update(deepcopy(record))
    saved["published"] = published
    shared = dataset["extension"]["nordicintel"]
    for key in ("data_url", "metadata_url", "source_url", "paths"):
        shared.pop(key, None)
    shared.update(v1_listing_fields(record))
    return dataset


def document_identity(document: dict) -> dict:
    if document.get("class") == "dataset":
        extension = document["extension"]
        return extension.get("nordicintel", extension.get("metadata_harvest", {}))
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

    def __init__(
        self, directory: str | Path, *, identify=document_identity, compressed=False
    ):
        self.directory = Path(directory)
        self.identify = identify
        self.compressed = compressed
        self.records: dict[str, dict] = {}
        self._legacy: dict[Path, dict] = {}
        for path in sorted(
            [*self.directory.glob("*.jsonl"), *self.directory.glob("*.jsonl.gz")]
        ):
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    document = json.loads(line)
                    if document.get("@type") == "dcat:Dataset":
                        model = Dataset.from_jsonld(document)
                        for contribution in contribution_documents(model):
                            name = filename(self.identify(contribution))
                            if name in self.records:
                                raise ValueError(
                                    f"Duplicate source contribution in {path}:{number}"
                                )
                            self.records[name] = contribution
                        continue
                    name = filename(self.identify(document))
                    if collection_filename(
                        self.identify(document)
                    ) != path.name.removesuffix(".gz"):
                        raise ValueError(f"Wrong provider/language in {path}:{number}")
                    if name in self.records:
                        raise ValueError(f"Duplicate dataset in {path}:{number}")
                    self.records[name] = document
        for path in sorted(self.directory.glob("*.json")):
            document = read(path)
            name = filename(self.identify(document))
            if path.name != name:
                raise ValueError(f"Legacy dataset filename mismatch: {path.name}")
            if name in self.records and self.records[name] != document:
                raise ValueError(f"Conflicting legacy and JSONL dataset: {name}")
            self.records[name] = document
            self._legacy[path] = deepcopy(document)

    def save(self) -> None:
        """Write deterministic JSONL; unchanged groups retain their exact bytes."""
        if self.directory.name == "metadata":
            for path, original in self._legacy.items():
                if read(path) != original:
                    raise ValueError(f"Legacy input changed during migration: {path}")
            for name, document in self.records.items():
                if filename(self.identify(document)) != name:
                    raise ValueError("Dataset key does not match identity")
            write_metadata(self.directory.parent, self.records.values())
            for path in self._legacy:
                path.unlink()
            self._legacy.clear()
            return
        groups: dict[str, list[dict]] = {}
        for name, document in sorted(self.records.items()):
            if filename(self.identify(document)) != name:
                raise ValueError(f"Dataset key does not match identity: {name}")
            groups.setdefault(
                collection_filename(self.identify(document))
                + (".gz" if self.compressed else ""),
                [],
            ).append(document)
        self.directory.mkdir(parents=True, exist_ok=True)
        staged: dict[Path, Path] = {}
        try:
            for name, documents in groups.items():
                path = self.directory / name
                temporary = self.directory / f".{uuid4().hex}.tmp"
                staged[path] = temporary
                with temporary.open("wb") as raw:
                    with (
                        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
                        if self.compressed
                        else nullcontext(raw)
                    ) as stream:
                        for document in documents:
                            stream.write(
                                (
                                    json.dumps(
                                        document,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                        allow_nan=False,
                                    )
                                    + "\n"
                                ).encode("utf-8")
                            )
                    raw.flush()
                    os.fsync(raw.fileno())
                opener = gzip.open if self.compressed else open
                with opener(temporary, "rt", encoding="utf-8") as stream:
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
                opener = gzip.open if self.compressed else open
                with opener(self.directory / name, "rt", encoding="utf-8") as stream:
                    if [json.loads(line) for line in stream] != documents:
                        raise ValueError("Written JSONL failed content verification")
            for path in self._legacy:
                path.unlink()
            self._legacy.clear()
            for path in [
                *self.directory.glob("*.jsonl"),
                *self.directory.glob("*.jsonl.gz"),
            ]:
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

"""Accepted collections: one deterministic gzip JSONL file of worker documents per
provider under ``data/<adapter>/metadata``."""

import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4


def read_records(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                raise ValueError(f"Truncated record at {path}:{number}")
            yield json.loads(line)


def collection_name(provider: str) -> str:
    digest = hashlib.sha256(provider.encode()).hexdigest()[:12]
    return f"{quote(provider, safe='')[:65]}__{digest}.jsonl.gz"


def _identity(document: dict) -> tuple[str, str, str]:
    shared = document["extension"]["nordicintel"]
    return shared["provider_code"], shared["language"], shared["dataset_code"]


def write_metadata(root: Path, documents, *, empty_providers=()) -> list[Path]:
    """Stage every provider collection, verify it, then replace the accepted files.

    A provider listed in ``empty_providers`` or already present as a collection
    keeps an (empty) collection: an empty file is explicit accepted state, not a
    missing one. Unchanged collections keep their exact bytes.
    """
    root = Path(root)
    directory = root / "metadata"
    directory.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[dict]] = {provider: [] for provider in empty_providers}
    for path in directory.glob("*.jsonl*"):
        groups.setdefault(unquote(path.name.split("__", 1)[0]), [])
    for document in documents:
        groups.setdefault(_identity(document)[0], []).append(document)
    written, staged = [], {}
    try:
        for provider, items in sorted(groups.items()):
            items.sort(key=_identity)
            if len({_identity(d) for d in items}) != len(items):
                raise ValueError(f"Duplicate dataset in {provider} collection")
            temporary = directory / f".{uuid4().hex}.tmp"
            staged[directory / collection_name(provider)] = temporary
            with (
                temporary.open("wb") as raw,
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream,
            ):
                for document in items:
                    stream.write((_encoded(document) + "\n").encode("utf-8"))
            with gzip.open(temporary, "rt", encoding="utf-8") as stream:
                if [json.loads(line) for line in stream] != items:
                    raise ValueError("Serialization changed document content")
        for target, temporary in staged.items():
            if not target.exists() or _digest(temporary) != _digest(target):
                temporary.replace(target)
            written.append(target)
        for old in directory.glob("*.jsonl*"):
            if old not in written:
                old.unlink()
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return written


def _encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()

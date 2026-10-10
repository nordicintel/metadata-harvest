"""Accepted multilingual models, with an explicit legacy worker compatibility reader."""

import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote

from metadata_harvest.models import Dataset, HarvestContribution
from metadata_harvest.profile_migration import read_dataset
from metadata_harvest.sources import inventory
from metadata_harvest.statdcat import assemble, convert_dataset, legacy_document


def adapter_for(document: dict) -> str:
    shared = document["extension"]["nordicintel"]
    matches = {
        entry["adapter"]
        for entry in inventory()
        if entry["source_code"] == shared["provider_code"]
    }
    from metadata_harvest.registry import ADAPTERS

    present = set(document["extension"]) & {
        name for name, spec in ADAPTERS.items() if spec.available
    }
    if len(present) == 1:
        return present.pop()
    if len(matches) == 1:
        return matches.pop()
    raise ValueError("Source requires an explicit registered adapter")


def contribution_documents(dataset: Dataset):
    """Expose independently replaceable old task records for transition workers."""
    for distribution in dataset.distributions:
        if distribution.retrieval:
            yield legacy_document(distribution, dataset)


def read_records(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                raise ValueError(f"Truncated record at {path}:{number}")
            yield json.loads(line)


def load_contributions(directory: Path):
    """Read accepted profiles explicitly; newly collected worker artifacts stay strict."""
    for path in sorted(Path(directory).glob("*.jsonl*")):
        for document in read_records(path):
            if document.get("@type") == "dcat:Dataset":
                yield from contribution_documents(read_dataset(document))
            else:
                yield document


def canonical_provider(provider: str) -> str:
    owners = {
        item["owner"]["code"]
        for item in inventory()
        if item["source_code"] == provider and item["owner"]
    }
    if len(owners) > 1:
        raise ValueError("Conflicting organisation identity")
    return next(iter(owners), provider)


def collection_name(provider: str) -> str:
    provider = canonical_provider(provider)
    digest = hashlib.sha256(provider.encode()).hexdigest()[:12]
    return f"{quote(provider, safe='')[:65]}__multilingual__{digest}.jsonl.gz"


def model_contributions(documents, *, adapter=None):
    for document in documents:
        if isinstance(document, HarvestContribution):
            yield document
        elif isinstance(document, Dataset):
            for distribution in document.distributions:
                if distribution.retrieval:
                    part = document.model_copy(deep=True)
                    part.distributions = [distribution]
                    yield HarvestContribution(
                        source=distribution.retrieval.source,
                        dataset=part,
                        codelists=[
                            c.dimension.code_list
                            for c in distribution.structure.components
                        ],
                    )
        elif document.get("@type") == "dcat:Dataset":
            yield from model_contributions([read_dataset(document)], adapter=adapter)
        else:
            yield convert_dataset(document, adapter=adapter or adapter_for(document))


def write_metadata(
    root: Path, documents, *, adapter=None, empty_providers=()
) -> list[Path]:
    """Stage all provider collections before replacing verified accepted files."""
    root = Path(root)
    directory = root / "metadata"
    directory.mkdir(parents=True, exist_ok=True)
    providers = {canonical_provider(p) for p in empty_providers}
    # Existing empty collections are explicit accepted state, not missing files.
    providers.update(
        canonical_provider(unquote(p.name.split("__", 1)[0]))
        for p in directory.glob("*.jsonl*")
    )
    written = []
    with TemporaryDirectory(prefix=".statdcat-", dir=root) as temporary:
        temporary = Path(temporary)
        with closing(sqlite3.connect(temporary / "contributions.sqlite")) as db:
            db.executescript(
                "CREATE TABLE contributions(provider TEXT, id TEXT, doc TEXT); CREATE INDEX identity ON contributions(provider,id);"
            )
            for contribution in model_contributions(documents, adapter=adapter):
                provider = canonical_provider(contribution.source.provider_code)
                providers.add(provider)
                db.execute(
                    "INSERT INTO contributions VALUES (?,?,?)",
                    (
                        provider,
                        contribution.dataset.id,
                        _encoded(contribution.dataset.to_jsonld()),
                    ),
                )
            staged = []
            for provider in sorted(providers):
                path = temporary / collection_name(provider)
                expected = hashlib.sha256()
                with (
                    path.open("wb") as raw,
                    gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw, mtime=0
                    ) as stream,
                ):
                    for (identity,) in db.execute(
                        "SELECT DISTINCT id FROM contributions WHERE provider=? ORDER BY id",
                        (provider,),
                    ).fetchall():
                        models = (
                            Dataset.from_jsonld(json.loads(row[0]))
                            for row in db.execute(
                                "SELECT doc FROM contributions WHERE provider=? AND id=?",
                                (provider, identity),
                            )
                        )
                        datasets, _ = assemble(
                            list(model_contributions(models)), include_codelists=False
                        )
                        for dataset in datasets:
                            value = (_encoded(dataset.to_jsonld()) + "\n").encode()
                            expected.update(value)
                            stream.write(value)
                actual = hashlib.sha256()
                for record in read_records(path):
                    value = (
                        _encoded(Dataset.from_jsonld(record).to_jsonld()) + "\n"
                    ).encode()
                    actual.update(value)
                if actual.digest() != expected.digest():
                    raise ValueError("Accepted model serialization changed content")
                staged.append(path)
            for path in staged:
                destination = directory / path.name
                if not destination.exists() or _digest(path) != _digest(destination):
                    path.replace(destination)
                written.append(destination)
            for old in directory.glob("*.jsonl*"):
                if old not in written:
                    old.unlink()
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


def as_contributions(value, *, adapter: str) -> list[HarvestContribution]:
    """Typed adapter boundary; native parsers may emit one, many, or no datasets."""
    documents = [] if value is None else value if isinstance(value, list) else [value]
    return list(model_contributions(documents, adapter=adapter))

"""Group stored EntryScape table structures into datasets and map them."""

import logging
from collections.abc import Iterator
from pathlib import Path

from apps.harvest.metadata_adapters.entryscape.datasets import build_datasets
from apps.harvest.metadata_adapters.entryscape.mapping import to_document
from apps.harvest.source_store import SourceStore, source_key
from apps.harvest.storage import document_identity, filename
from registry import ServiceBinding

logger = logging.getLogger("entryscape_logger")


def stored_key(record: dict) -> str:
    """The source-store key of a discovered table."""
    return source_key(record["provider_code"], record["dataset_code"], "sv")


def withheld_keys(state: dict) -> set[str]:
    return set()


def table_documents(store: SourceStore) -> list[dict]:
    """The trimmed structure documents of every stored table, in key order."""
    documents = []
    for key in sorted(store.entries()):
        document = store.get(key)["derived"].get("document")
        if document:
            documents.append(document)
    return documents


def rebuild(documents: list[dict], bindings: list[ServiceBinding]) -> tuple[dict, dict]:
    """Group table structure documents into datasets and map the cubes.

    Returns the documents keyed by collection filename and a summary with the
    dropped tables and list-only datasets.
    """
    by_code = {b.source_code: b for b in bindings}
    logger.info("Grouping %d table structures into datasets", len(documents))
    datasets, dropped = build_datasets(documents)
    by_url = {d["extension"]["metadata_harvest"]["table_url"]: d for d in documents}
    mapped, lists = {}, []
    for dataset in datasets:
        members = [by_url[url] for url in dataset["tables"]]
        dataset["records"] = [m["extension"]["metadata_harvest"] for m in members]
        dataset["cells_unique"] = all(
            m["extension"]["rowstore"].get("dimension_key_unique_in_sample", False)
            for m in members
        )
        if dataset["kind"] != "cube":
            lists.append(dataset["dataset_code"])
            logger.debug(
                "Dataset %s has no dimension; kept as a list-only dataset, "
                "not published",
                dataset["dataset_code"],
            )
            continue
        document = to_document(dataset, by_code.get(dataset["provider_code"], {}))
        mapped[filename(document_identity(document))] = document
    logger.info(
        "Mapped %d cube datasets (%d list-only datasets not published)",
        len(mapped),
        len(lists),
    )
    return mapped, {"dropped": dropped, "lists": lists, "datasets": len(mapped)}


def published_documents(root: Path, bindings: list[ServiceBinding]) -> list[dict]:
    """The dataset documents of ``data/entryscape`` (live checks, migrations)."""
    store = SourceStore(Path(root) / "source", "entryscape")
    mapped, _ = rebuild(table_documents(store), bindings)
    return list(mapped.values())


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    try:
        mapped, _ = rebuild(table_documents(store), bindings)
    except Exception as error:
        for key in sorted(store.entries()):
            yield key, error
        return
    for name in sorted(mapped):
        document = mapped[name]
        shared = document["extension"]["nordicintel"]
        yield (
            source_key(
                shared["provider_code"], shared["dataset_code"], shared["language"]
            ),
            document,
        )

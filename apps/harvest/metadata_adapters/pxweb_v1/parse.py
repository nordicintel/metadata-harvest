"""Rebuild PXWeb v1 worker documents from stored metadata, samples and listings."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.pxweb_v1.metadata import (
    assemble_document,
    listed_paths,
)
from apps.harvest.source_store import SourceStore, split_key
from apps.harvest.storage import JsonlStore, filename, refresh_identity


def withheld_keys(state: dict) -> set[str]:
    return set()


def document_from(key: str, envelope: dict, listings: dict) -> dict:
    """The worker document of one table, refreshed with its current listing row."""
    provider, _, _ = split_key(key)
    responses, derived = envelope["responses"], envelope["derived"]
    table = responses["table"]
    document = assemble_document(table, provider, envelope)
    current = listings.get(filename({**table, "provider_code": provider}))
    if current is not None and current["published"] == table["published"]:
        document = refresh_identity(
            document, listed_paths(current, derived.get("paths"))
        )
    return document


def documents(
    store: SourceStore, state: dict, providers: list[dict], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    directory = store.root.parent / "table-records"
    listings = JsonlStore(directory).records if directory.exists() else {}
    for key in sorted(store.entries()):
        try:
            yield key, document_from(key, store.get(key), listings)
        except Exception as error:
            yield key, error

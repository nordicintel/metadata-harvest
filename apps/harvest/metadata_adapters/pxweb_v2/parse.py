"""Rebuild PXWeb v2 worker documents from stored native metadata."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.pxweb_v1.mapping import map_metadata
from apps.harvest.source_store import SourceStore
from registry import ServiceBinding


def withheld_keys(state: dict) -> set[str]:
    return set()


def document_from(envelope: dict) -> dict:
    responses, derived = envelope["responses"], envelope["derived"]
    return map_metadata(
        responses["metadata"],
        responses["table"],
        "pxweb_v2",
        max_cells=derived.get("max_cells"),
    )


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    for key in sorted(store.entries()):
        try:
            yield key, document_from(store.get(key))
        except Exception as error:
            yield key, error

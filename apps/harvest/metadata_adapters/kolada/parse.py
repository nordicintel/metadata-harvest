"""Rebuild Kolada worker documents from stored KPI rows, coverage and listings."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.kolada.client import PROVIDER_CODE
from apps.harvest.metadata_adapters.kolada.mapping import (
    build_dataset,
    load_municipalities,
    reference_rows,
)
from apps.harvest.source_store import SourceStore, source_key, split_key
from registry import ServiceBinding

LISTINGS = ("ou", "municipality_groups", "kpi_groups")


def stored_key(code: str) -> str:
    """The source-store key of a ``<scope>/<kpi>`` code."""
    return source_key(PROVIDER_CODE, code, "sv")


def withheld_keys(state: dict) -> set[str]:
    return set()


def references_from(store: SourceStore) -> dict:
    references = {"municipality": load_municipalities()}
    for name in LISTINGS:
        rows = store.get_shared(PROVIDER_CODE, f"{name}.json")
        references[name] = reference_rows(rows) if rows else {}
    return references


def document_from(code: str, envelope: dict, references: dict) -> dict:
    scope = code.split("/", 1)[0]
    derived = envelope["derived"]
    return build_dataset(
        envelope["responses"]["kpi"],
        scope,
        derived["coverage"],
        references,
        updated=derived["updated"],
    )


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    try:
        references = references_from(store)
    except Exception as error:
        for key in sorted(store.entries()):
            yield key, error
        return
    for key in sorted(store.entries()):
        try:
            _, code, _ = split_key(key)
            yield key, document_from(code, store.get(key), references)
        except Exception as error:
            yield key, error

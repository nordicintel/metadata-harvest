"""Rebuild Tillväxtverket worker documents from stored distinct-value responses.

The data.europa.eu description comes from the accepted portal snapshot
(``data/data_eu``), never from a harvest-time request.
"""

from collections.abc import Iterator
from pathlib import Path

from apps.harvest.metadata_adapters.tillvaxtverket.api import dcat_id
from apps.harvest.metadata_adapters.tillvaxtverket.mapping import (
    apply_dcat,
    build_cube,
)
from apps.harvest.metadata_adapters.tillvaxtverket.source import (
    BY_NAME,
    Spec,
    parse_categories,
    parse_dcat,
    parse_labels,
    parse_periods,
    parse_probe,
    parse_regions,
)
from apps.harvest.metadata_adapters.tillvaxtverket.state import PROVIDER_CODE
from apps.harvest.source_store import SourceStore, source_key, split_key

LABELS = "labels.csv"


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("datasets", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def portal_records(portal: Path | None) -> dict[str, dict]:
    """Snapshot records by id; empty when no accepted snapshot is available."""
    if portal is None or not (Path(portal) / "state.json").exists():
        return {}
    from apps.portal.snapshot import accepted_records

    return {record["id"]: record for record in accepted_records(Path(portal))}


def stored_labels(store: SourceStore, key: str) -> dict[str, str] | None:
    for relative in store.entries()[key]["files"]:
        if relative.endswith(f".{LABELS}"):
            return parse_labels(store.read_file(relative))
    return None


def document_from(spec: Spec, responses: dict, labels, dcat_body) -> dict:
    probe = parse_probe(spec, responses["columns"], responses["load_dates"])
    regions = parse_regions(responses["regions"])
    categories = {
        dimension.id: parse_categories(dimension, responses["dimensions"][dimension.id])
        for dimension in spec.dimensions
    }
    periods = parse_periods(spec, responses["periods"])
    cube = build_cube(spec, probe, regions, categories, periods, labels)
    dcat = parse_dcat({"result": dcat_body}) if dcat_body is not None else None
    return apply_dcat(cube, spec, dcat)


def documents(
    store: SourceStore, state: dict, providers: list[dict], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    try:
        portal_index = portal_records(portal)
    except Exception as error:
        for key in sorted(store.entries()):
            yield key, error
        return
    for key in sorted(store.entries()):
        try:
            _, code, _ = split_key(key)
            spec = BY_NAME.get(code.split("/", 1)[1])
            if spec is None:
                raise LookupError(f"{code} is not a registered dataset")
            envelope = store.get(key)
            yield (
                key,
                document_from(
                    spec,
                    envelope["responses"],
                    stored_labels(store, key),
                    portal_index.get(dcat_id(spec.name)),
                ),
            )
        except Exception as error:
            yield key, error

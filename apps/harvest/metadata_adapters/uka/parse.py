"""Rebuild UKÄ worker documents from stored catalogue rows, totals and scans."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.uka.mapping import Scan, build_dataset
from apps.harvest.metadata_adapters.uka.source import parse_catalogue, parse_totals
from apps.harvest.metadata_adapters.uka.state import PROVIDER_CODE
from apps.harvest.source_store import SourceStore, source_key


def stored_key(code: str) -> str:
    return source_key(PROVIDER_CODE, str(code), "sv")


def withheld_keys(state: dict) -> set[str]:
    return {
        stored_key(code)
        for code, info in state.get("indicators", {}).items()
        if info.get("status") == "withheld"
    }


def document_from(responses: dict, derived: dict) -> dict:
    """The worker document of one indicator from its stored responses and scan."""
    catalogue = responses["catalogue"]
    (entry,) = parse_catalogue({catalogue["group"]: [catalogue["row"]]})
    indicator = parse_totals(responses["totals"], entry.id)
    return build_dataset(entry, indicator, Scan.from_summary(derived["scan"]))


def documents(
    store: SourceStore, state: dict, providers: list[dict], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    for key in sorted(store.entries(), key=lambda k: int(k.split("/")[1])):
        try:
            envelope = store.get(key)
            yield key, document_from(envelope["responses"], envelope["derived"])
        except Exception as error:
            yield key, error

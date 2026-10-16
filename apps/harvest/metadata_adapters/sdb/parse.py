"""Rebuild SDB worker documents from stored listings, variables and probes."""

from collections.abc import Iterator
from urllib.parse import quote

from apps.harvest.metadata_adapters.sdb.mapping import (
    PROVIDER_CODE,
    Subject,
    build_dataset,
    time_unit_from_swedish_label,
)
from apps.harvest.metadata_adapters.sdb.probe import reconcile
from apps.harvest.source_store import SourceStore, source_key, split_key
from registry import ServiceBinding


def probe_name(subject: str) -> str:
    return f"probe/{quote(subject, safe='')}.json"


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("subjects", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def document_from(
    row: dict, language: str, time_unit, responses: dict, probe_raw: dict
) -> dict:
    """The worker document of one subject from its stored responses."""
    variables, categories = responses["variables"], responses["categories"]
    probe = reconcile(probe_raw, variables, categories)
    return build_dataset(
        Subject(row["namn"], row["text"].strip(), time_unit),
        language,
        variables,
        categories,
        result_dimensions=probe.result_dimensions,
        added_periods=probe.added_periods,
        source_record=row,
    )


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    swedish = store.get_shared(PROVIDER_CODE, "listing.sv.json") or []
    time_units = {
        row["namn"]: time_unit_from_swedish_label(row["text"])
        for row in swedish
        if isinstance(row, dict) and isinstance(row.get("namn"), str)
    }
    for key in sorted(store.entries()):
        try:
            _, code, language = split_key(key)
            envelope = store.get(key)
            probe_raw = store.get_shared(PROVIDER_CODE, probe_name(code))
            if probe_raw is None:
                raise LookupError(f"No stored result probe for {code}")
            responses = envelope["responses"]
            yield (
                key,
                document_from(
                    responses["listing_row"],
                    language,
                    time_units.get(code),
                    responses,
                    probe_raw,
                ),
            )
        except Exception as error:
            yield key, error

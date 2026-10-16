"""Rebuild Riksbank worker documents from stored catalogue records and probes."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.riksbank.client import PROVIDER_CODE
from apps.harvest.metadata_adapters.riksbank.mapping import (
    build_mpd,
    build_swea_group,
    build_swestr,
)
from apps.harvest.source_store import SourceStore, source_key
from registry import ServiceBinding

SHARED_NAMES = {
    "swea": ("swea_groups.json", "swea_series.json"),
    "mpd": ("mpd_series_ids.json", "mpd_policy_rounds.json"),
    "swestr": ("swestr_latest.json",),
}


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("datasets", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def document_from(record: dict, responses: dict) -> dict:
    """The worker document of one dataset from its discovery record and probe."""
    if record["api"] == "swea":
        return build_swea_group(record, responses["latest"])
    if record["api"] == "mpd":
        return build_mpd(record, responses["vintage"])
    return build_swestr(responses["latest"])


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    for key in sorted(store.entries()):
        try:
            envelope = store.get(key)
            yield (
                key,
                document_from(envelope["derived"]["record"], envelope["responses"]),
            )
        except Exception as error:
            yield key, error

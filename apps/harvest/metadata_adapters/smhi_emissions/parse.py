"""Rebuild SMHI emissions worker documents from the stored tree and probes."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.smhi_emissions.mapping import build_dataset
from apps.harvest.metadata_adapters.smhi_emissions.source import (
    Substance,
    parse_filter_options,
    parse_probe,
)
from apps.harvest.metadata_adapters.smhi_emissions.state import PROVIDER_CODE
from apps.harvest.source_store import SourceStore, source_key, split_key
from registry import ServiceBinding
from retrieval.data_adapters.smhi_emissions import DATASET_PREFIX


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("datasets", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    tree = store.get_shared(PROVIDER_CODE, "filteroptions.json")
    try:
        options = parse_filter_options(tree) if tree is not None else None
    except Exception as error:
        options, tree_error = None, error
    else:
        tree_error = LookupError("No stored getfilteroptions tree")
    for key in sorted(store.entries()):
        try:
            if options is None:
                raise tree_error
            _, code, _ = split_key(key)
            substance = code.removeprefix(DATASET_PREFIX)
            envelope = store.get(key)
            if substance in options.substances:
                listed = options.substances[substance]
            else:  # quarantined or unlisted now: the identity stored with the probe
                record = envelope["derived"]["substance"]
                listed = Substance(
                    record["code"],
                    record["substance_id"],
                    record["long_name"],
                    tuple(record["listed_in"]),
                )
            body = envelope["responses"]["getdata"]
            probe = parse_probe(body, options, substance)
            yield key, build_dataset(listed, options, probe)
        except Exception as error:
            yield key, error

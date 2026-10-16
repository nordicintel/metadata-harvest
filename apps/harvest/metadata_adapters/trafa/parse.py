"""Rebuild Trafikanalys worker documents from stored structures and probes."""

from collections.abc import Iterator

from apps.harvest.metadata_adapters.trafa.client import PROVIDER_CODE
from apps.harvest.metadata_adapters.trafa.mapping import build_dataset, parse_probe
from apps.harvest.metadata_adapters.trafa.source import (
    parse_combinations,
    parse_product,
    product_list,
    select_combinations,
)
from apps.harvest.source_store import SourceStore, source_key, split_key
from registry import ServiceBinding


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("datasets", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def _products(store: SourceStore, product_id: str, name: str):
    swedish = parse_product(
        store.get_shared(PROVIDER_CODE, f"structure.{product_id}.sv.json") or {}, name
    )
    english_body = store.get_shared(PROVIDER_CODE, f"structure.{product_id}.en.json")
    english = parse_product(english_body, name) if english_body else None
    html = store.get_shared(PROVIDER_CODE, f"documentation.{product_id}.html")
    if html is None:
        raise LookupError(f"No stored documentation page for {product_id}")
    selection = select_combinations(swedish, parse_combinations(html))
    return swedish, english, {c.dataset_code: c for c in selection.datasets}


def _document(envelope: dict, combination, swedish, english) -> dict:
    language = envelope["language"]
    responses = envelope["responses"]
    if language == "sv":
        return build_dataset(
            combination, swedish, "sv", parse_probe(responses["probe"])
        )
    if english is None:
        raise LookupError("No stored English structure")
    return build_dataset(
        combination,
        english,
        language,
        parse_probe(responses["probe"]),
        reference=swedish,
        reference_probe=parse_probe(responses["reference_probe"]),
    )


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    listing = store.get_shared(PROVIDER_CODE, "products.json")
    entries = {e["unique_id"]: e for e in product_list(listing)} if listing else {}
    by_product: dict[str, list[str]] = {}
    for key in store.entries():
        _, code, _ = split_key(key)
        by_product.setdefault(code.split("|", 1)[0], []).append(key)
    for product_id, keys in sorted(by_product.items()):
        try:
            if product_id not in entries:
                raise LookupError(f"Product {product_id} is not in the stored list")
            swedish, english, combinations = _products(
                store, product_id, entries[product_id]["name"]
            )
        except Exception as error:
            for key in keys:
                yield key, error
            continue
        for key in sorted(keys):
            try:
                envelope = store.get(key)
                combination = combinations.get(envelope["identifier"])
                if combination is None:
                    raise LookupError("Combination is no longer listed for the product")
                yield key, _document(envelope, combination, swedish, english)
            except Exception as error:
                yield key, error

"""Rebuild Försäkringskassan worker documents from stored meta, texts and rows."""

from collections.abc import Iterator
from dataclasses import asdict
from urllib.parse import quote

from apps.harvest.metadata_adapters.forsakringskassan.mapping import build_dataset
from apps.harvest.metadata_adapters.forsakringskassan.source import (
    Listed,
    Report,
    Table,
    fingerprint,
    parse_meta,
    text_entries,
)
from apps.harvest.metadata_adapters.forsakringskassan.state import PROVIDER_CODE
from apps.harvest.source_store import SourceStore, source_key, split_key
from registry import ServiceBinding


def meta_name(report: str) -> str:
    return f"meta/{quote(report, safe='')}.json"


def table_record(table: Table) -> dict:
    return {**asdict(table), "dimensions": list(table.dimensions)}


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("datasets", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def document_from(envelope: dict, report: Report, meta: dict, texts: dict) -> dict:
    """The worker document of one table from its stored responses."""
    listing = envelope["responses"]["listing"]
    listed = Listed(listing["report"], listing["area"], listing["group"])
    name = envelope["identifier"].split("/", 1)[1]
    table = next((t for t in report.tables if t.name == name), None)
    if table is None:  # quarantined or vanished since: the stored table definition
        record = envelope["derived"]["table"]
        table = Table(
            record["key"], record["label"], record["name"], tuple(record["dimensions"])
        )
    return build_dataset(
        report,
        table,
        listed,
        texts,
        meta_sha256=fingerprint(meta),
        texts_sha256=fingerprint(text_entries(texts, report)),
    )


def documents(
    store: SourceStore, state: dict, bindings: list[ServiceBinding], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    texts = store.get_shared(PROVIDER_CODE, "texts.json")
    by_report: dict[str, list[str]] = {}
    for key in store.entries():
        _, code, _ = split_key(key)
        by_report.setdefault(code.split("/", 1)[0], []).append(key)
    for name, keys in sorted(by_report.items()):
        try:
            if texts is None:
                raise LookupError("No stored texts")
            meta = store.get_shared(PROVIDER_CODE, meta_name(name))
            if meta is None:
                raise LookupError(f"No stored meta.json for report {name}")
            area = store.get(keys[0])["responses"]["listing"]["area"]
            report = parse_meta(meta, name, area)
        except Exception as error:
            for key in sorted(keys):
                yield key, error
            continue
        for key in sorted(keys):
            try:
                yield key, document_from(store.get(key), report, meta, texts)
            except Exception as error:
                yield key, error

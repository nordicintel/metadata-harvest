"""Rebuild analysverktyg worker documents from stored cubes and harvest context."""

from collections.abc import Iterator
from dataclasses import asdict

from apps.harvest.metadata_adapters.dataanalys.api import result_rows
from apps.harvest.metadata_adapters.dataanalys.mapping import build_dataset
from apps.harvest.metadata_adapters.dataanalys.source import (
    Area,
    Dashboard,
    Link,
    parse_cube,
)
from apps.harvest.metadata_adapters.dataanalys.state import PROVIDER_CODE
from apps.harvest.source_store import SourceStore, source_key


def withheld_keys(state: dict) -> set[str]:
    result = set()
    for key, info in state.get("datasets", {}).items():
        if info.get("status") == "withheld":
            language, code = key.split("/", 1)
            result.add(source_key(PROVIDER_CODE, code, language))
    return result


def link_record(link: Link) -> dict:
    """JSON form of a resolved link (dashboard tags as a sorted list)."""
    dashboard = None
    if link.dashboard is not None:
        dashboard = {**asdict(link.dashboard), "tags": sorted(link.dashboard.tags)}
    return {"resolution": link.resolution, "dashboard": dashboard}


def link_from(record: dict) -> Link:
    dashboard = record.get("dashboard")
    return Link(
        record["resolution"],
        None
        if dashboard is None
        else Dashboard(
            dashboard["id"],
            dashboard["slug"],
            dashboard["title"],
            dashboard["published"],
            frozenset(dashboard["tags"]),
        ),
    )


def document_from(identifier: str, responses: dict, derived: dict) -> dict:
    """The worker document of one indicator from its stored cube and context."""
    code = identifier.split("|", 1)[1]
    columns = derived["columns"]
    cube = parse_cube(result_rows(responses["cube"]), columns)
    if cube is None:
        raise LookupError("no Riket or Region rows; out of scope")
    return build_dataset(
        Area(**derived["area"]),
        code,
        derived["row"],
        cube,
        columns,
        link_from(derived["link"]),
        subject_label=derived["subject_label"],
    )


def documents(
    store: SourceStore, state: dict, providers: list[dict], *, portal=None
) -> Iterator[tuple[str, dict | Exception]]:
    for key in sorted(store.entries()):
        try:
            envelope = store.get(key)
            yield (
                key,
                document_from(
                    envelope["identifier"], envelope["responses"], envelope["derived"]
                ),
            )
        except Exception as error:
            yield key, error

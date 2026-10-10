"""Organisation and catalog identities, separate from shared adapter services."""

import json
import re
from functools import cache
from importlib.resources import files
from urllib.parse import quote

from metadata_harvest.models import Agent, Catalog, DataService, SourceIdentity


@cache
def inventory() -> list[dict]:
    return json.loads(
        files("metadata_harvest")
        .joinpath("source-inventory.json")
        .read_text(encoding="utf-8")
    )


def deployment(adapter: str, source_code: str) -> dict:
    for item in inventory():
        if item["adapter"] == adapter and item["source_code"] == source_code:
            return item
    from metadata_harvest.registry import get_adapter

    get_adapter(adapter)
    # Custom configured sources retain their native identity; never invent an owner.
    return {
        "adapter": adapter,
        "source_code": source_code,
        "owner": None,
        "owner_status": "unresolved",
        "catalog_key": source_code,
        "catalog_label": source_code,
        "endpoint": "",
        "databases": [],
    }


def _id(kind, *parts):
    return "urn:nordicintel:" + kind + ":" + ":".join(quote(p, safe="") for p in parts)


def source_entities(
    source: SourceIdentity, data_url: str | None, record: dict
) -> tuple[Catalog, DataService]:
    item = deployment(source.adapter, source.provider_code)
    language = source.language
    owner = item["owner"]
    publishers = (
        [Agent(id=_id("agent", owner["code"]), name={"sv": [owner["label"]]})]
        if owner
        else []
    )
    product = ""
    if source.adapter == "riksbank" and data_url:
        match = re.match(
            r"^https://api.riksbank.se/(swea|monetary_policy_data)/v1(?:/|$)", data_url
        )
        if match:
            product = match[1]
    database = (
        (record.get("database_id") if isinstance(record, dict) else None)
        or product
        or item["catalog_key"]
    )
    catalog = Catalog(
        id=_id(
            "catalog",
            owner["code"] if owner else source.provider_code,
            source.adapter,
            item["catalog_key"],
            *([str(database)] if database != item["catalog_key"] else []),
        ),
        title={
            language: [
                item["catalog_label"]
                + (": " + str(database) if database != item["catalog_key"] else "")
            ]
        },
        publisher=publishers,
    )
    service = adapter_service(source.adapter)
    return catalog, service


def adapter_service(adapter: str) -> DataService:
    """Exactly one shared data service per implemented adapter, never per host."""
    item = next((item for item in inventory() if item["adapter"] == adapter), None)
    if item is None:
        raise ValueError(f"Unknown data service: {adapter}")
    descriptions = {
        "pxweb_v1": "Queries multidimensional statistical tables through PxWeb API v1.",
        "pxweb_v2": "Queries multidimensional statistical tables through PxWeb API v2.",
        "entrysoft": "Queries tabular open data through EntryScape RowStore.",
        "kolada": "Retrieves municipal and organisational-unit indicators through Kolada API v3.",
        "uka": "Retrieves higher-education statistical indicators through the UKÄ statistics API.",
        "sdb": "Retrieves statistical tables through Socialstyrelsen's statistics database API.",
        "dataanalys": "Retrieves statistical indicators through Socialstyrelsen's analysis API.",
        "trafa": "Retrieves transport statistics through the Trafikanalys API.",
        "smhi_emissions": "Retrieves emissions statistics through the national emissions database API.",
        "forsakringskassan": "Retrieves social-insurance statistics through Försäkringskassan's statistics API.",
        "riksbank": "Retrieves monetary-policy, interest-rate and exchange-rate series through Riksbank APIs.",
        "tillvaxtverket": "Retrieves statistical tables through Tillväxtverket's open-data API.",
    }
    return DataService(
        id=_id("service", adapter),
        adapter=adapter,
        title={"en": [item["software"]]},
        description={"en": [descriptions[adapter]]},
    )

"""Retrieve native metadata and referenced codelists, never observations."""

from copy import deepcopy
from urllib.parse import quote

from harvesting.adapters.pxweb_v2.client import Client
from harvesting.adapters.pxweb_v2.discovery import table_record
from harvesting.adapters.pxweb_v2.validation import (
    validate_codelist,
    validate_metadata,
)
from harvesting.mapping import map_metadata


async def fetch_metadata(record: dict, *, client: Client | None = None) -> dict:
    """Fetch full categories, retrying one catalogue/metadata update race."""
    if client is None:
        async with Client(record["base_api_url"]) as owned:
            owned.configure(await owned.get("config"))
            return await fetch_metadata(record, client=owned)
    current = deepcopy(record)
    endpoint = f"tables/{quote(record['dataset_code'], safe='')}"
    for attempt in range(2):
        dataset = await client.get(
            f"{endpoint}/metadata", lang=record["language"], defaultSelection="false"
        )
        if dataset.get("updated") != current["catalogue"]["updated"] and attempt == 0:
            table = await client.get(endpoint, lang=record["language"])
            if (
                table.get("language") != record["language"]
                or table.get("id") != record["dataset_code"]
            ):
                raise ValueError("Rediscovered table identity mismatch")
            current = table_record(
                {
                    "code": record["provider_code"],
                    "base_api_url": record["base_api_url"],
                },
                record["language"],
                table,
            )
            continue
        validate_metadata(dataset, current)
        return map_metadata(
            dataset, current, "pxweb_v2", max_cells=getattr(client, "max_cells", None)
        )
    raise ValueError("Source changed repeatedly during metadata retrieval")


async def fetch_codelist(
    code: str, language: str, *, client: Client | None = None
) -> dict:
    """Return the complete native SCB codelist payload in the requested language."""
    if client is None:
        async with Client() as owned:
            owned.configure(await owned.get("config"))
            return await fetch_codelist(code, language, client=owned)
    payload = await client.get(f"codelists/{quote(code, safe='')}", lang=language)
    validate_codelist(payload, code, language)
    return payload

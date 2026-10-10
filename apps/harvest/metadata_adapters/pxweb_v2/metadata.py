"""Retrieve native metadata and referenced codelists, never observations."""

import logging
from copy import deepcopy
from urllib.parse import quote

from apps.harvest.metadata_adapters.pxweb_v1.mapping import map_metadata
from apps.harvest.metadata_adapters.pxweb_v2.client import Client
from apps.harvest.metadata_adapters.pxweb_v2.discovery import table_record
from apps.harvest.metadata_adapters.pxweb_v2.validation import (
    validate_codelist,
    validate_metadata,
)

logger = logging.getLogger("pxweb_v2_logger")


async def fetch_metadata(record: dict, *, client: Client | None = None) -> dict:
    """Fetch full categories, retrying one catalogue/metadata update race."""
    if client is None:
        async with Client(record["base_api_url"]) as owned:
            owned.configure(await owned.get("config"))
            return await fetch_metadata(record, client=owned)
    current = deepcopy(record)
    endpoint = f"tables/{quote(record['dataset_code'], safe='')}"
    for attempt in range(2):
        logger.debug(
            "Fetching metadata for %s/%s (attempt %d/2)",
            record["language"],
            record["dataset_code"],
            attempt + 1,
        )
        dataset = await client.get(
            f"{endpoint}/metadata", lang=record["language"], defaultSelection="false"
        )
        if dataset.get("updated") != current["catalogue"]["updated"] and attempt == 0:
            logger.info(
                "Table %s/%s changed during harvest (catalogue updated %s, metadata "
                "updated %s); re-reading its catalogue entry",
                record["language"],
                record["dataset_code"],
                current["catalogue"]["updated"],
                dataset.get("updated"),
            )
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
        logger.debug(
            "Metadata for %s/%s validated (updated %s)",
            record["language"],
            record["dataset_code"],
            current["catalogue"]["updated"],
        )
        return map_metadata(
            dataset, current, "pxweb_v2", max_cells=getattr(client, "max_cells", None)
        )
    logger.warning(
        "Table %s/%s changed again after re-reading its catalogue entry; giving up",
        record["language"],
        record["dataset_code"],
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
    logger.debug("Fetching codelist %s (%s)", code, language)
    payload = await client.get(f"codelists/{quote(code, safe='')}", lang=language)
    validate_codelist(payload, code, language)
    logger.debug(
        "Codelist %s (%s) validated: %d values", code, language, len(payload["values"])
    )
    return payload

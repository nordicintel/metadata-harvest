"""Live observations through a caller-owned aiohttp session."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from retrieval._selection import retrieve_selection

logger = logging.getLogger("pxweb_v1_logger")
REQUIRED_METADATA = ("/extension/pxweb_v1", "/dimension")


async def retrieve(
    session: aiohttp.ClientSession,
    *,
    provider_code: str,
    dataset_code: str,
    language: str,
    selection: Mapping[str, Sequence[str]],
    data_url: str | None,
    metadata_url: str | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Retrieve the explicit base-category selection without writing observations."""
    logger.debug(
        "Retrieving %s/%s/%s: %d selected dimensions",
        provider_code,
        language,
        dataset_code,
        len(selection),
    )
    if provider_code == "sundsvall":
        # Its anonymous gateway redirects a first POST into a GET, dropping the
        # body. Establish the gateway session before sending the data selection.
        if not metadata_url:
            raise ValueError("Sundsvall requires the harvested metadata URL")
        logger.debug("Establishing Sundsvall gateway session via %s", metadata_url)
        async with session.get(metadata_url) as response:
            if response.status in {404, 410}:
                logger.warning(
                    "%s/%s no longer exists at the provider (HTTP %s)",
                    provider_code,
                    dataset_code,
                    response.status,
                )
                raise LookupError("Dataset no longer exists at the provider")
            response.raise_for_status()
            await response.read()

    def request(part):
        return {
            "json": {
                "query": [
                    {
                        "code": code,
                        "selection": {"filter": "item", "values": list(values)},
                    }
                    for code, values in part.items()
                ],
                "response": {"format": "json-stat2"},
            }
        }

    return await retrieve_selection(
        session,
        selection=selection,
        data_url=data_url,
        metadata_url=metadata_url,
        metadata=metadata,
        adapter="pxweb_v1",
        request=request,
    )

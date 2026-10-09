"""Live observations through a caller-owned aiohttp session."""

from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from metadata_harvest.retrieval import retrieve_selection

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
    if provider_code == "sundsvall":
        # Its anonymous gateway redirects a first POST into a GET, dropping the
        # body. Establish the gateway session before sending the data selection.
        if not metadata_url:
            raise ValueError("Sundsvall requires the harvested metadata URL")
        async with session.get(metadata_url) as response:
            if response.status in {404, 410}:
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

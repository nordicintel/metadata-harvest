"""Live observations through a caller-owned aiohttp session."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from retrieval._selection import retrieve_selection

logger = logging.getLogger("pxweb_v2_logger")

REQUIRED_METADATA = ("/extension/pxweb_v2", "/dimension")


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
    if provider_code != "scb" or language not in {"sv", "en"}:
        raise ValueError("PXWeb v2 currently supports SCB sv/en only")
    if data_url:
        url = urlsplit(data_url)
        query = [
            (k, v)
            for k, v in parse_qsl(url.query, keep_blank_values=True)
            if k not in {"lang", "outputFormat"}
        ]
        query.extend([("lang", language), ("outputFormat", "json-stat2")])
        data_url = urlunsplit(url._replace(query=urlencode(query)))

    logger.info(
        "Retrieving PXWeb v2 observations for %s/%s (%s): %d selected dimensions",
        provider_code,
        dataset_code,
        language,
        len(selection),
    )
    logger.debug("Data URL %s; metadata URL %s", data_url, metadata_url)

    def request(part):
        return {
            "json": {
                "selection": [
                    {"variableCode": code, "valueCodes": list(values)}
                    for code, values in part.items()
                ]
            },
        }

    return await retrieve_selection(
        session,
        selection=selection,
        data_url=data_url,
        metadata_url=metadata_url,
        metadata=metadata,
        adapter="pxweb_v2",
        request=request,
    )

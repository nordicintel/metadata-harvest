"""Sequential, paced, retrying HTTP client for harvesting from Tillväxtverket."""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode

import aiohttp

from harvest.lifecycle import Unreachable as _Unreachable
from retrieval.data_adapters.tillvaxtverket import (
    BASE_URL,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    REGION_DIMENSION,
    cbase_url,
)

logger = logging.getLogger("tillvaxtverket_logger")

TIMEOUT = aiohttp.ClientTimeout(total=120)
USER_AGENT = "nordicintel-metadata-harvest/tillvaxtverket"
Params = Sequence[tuple[str, str]] | None

__all__ = [
    "API_DOCUMENTATION_URL",
    "BASE_URL",
    "DCAT_API",
    "DCAT_PAGE",
    "MEASURE_DIMENSION",
    "PROVIDER_CODE",
    "REGION_DIMENSION",
    "cbase_url",
    "data_url",
    "dcat_id",
    "dcat_page_url",
    "dcat_url",
    "labels_url",
    "metadata_url",
]

API_DOCUMENTATION_URL = f"{BASE_URL}/api.yaml"
"""DivePort Web API description (OpenAPI), linked from every DCAT JSON distribution."""
DCAT_API = "https://data.europa.eu/api/hub/search/datasets"
DCAT_PAGE = "https://data.europa.eu/data/datasets"


def data_url(area: str, name: str) -> str:
    """The observation endpoint, as published in the DCAT JSON distribution."""
    return f"{cbase_url(area, name)}?{urlencode({'format': 'json'})}"


def metadata_url(area: str, name: str) -> str:
    """Columns and ``rowCount`` without rows."""
    return f"{cbase_url(area, name)}?{urlencode({'format': 'json', 'limit': 0})}"


def labels_url(area: str, name: str) -> str:
    """Tillväxtverket's column label file (``Name;Type;Length;Label``, Latin-1)."""
    return f"{BASE_URL}/data/{area}/{name}_variabeletiketter.csv"


def dcat_id(name: str) -> str:
    """The data.europa.eu dataset id harvested from Sveriges dataportal."""
    return f"https-oppnadata-tillvaxtverket-se-dataset_{name.lower()}"


def dcat_url(name: str) -> str:
    return f"{DCAT_API}/{dcat_id(name)}"


def dcat_page_url(name: str) -> str:
    return f"{DCAT_PAGE}/{dcat_id(name)}"


class Unreachable(_Unreachable):
    """A host did not answer a request successfully, after every retry."""


class TillvaxtverketClient:
    """Fetch resources one at a time through one session, at most one per second.

    Timeouts, connection errors, HTTP 429 and 5xx responses are retried with
    exponential backoff (``Retry-After`` is honoured when longer); exhausted
    retries and other HTTP errors raise :class:`Unreachable`. DivePort answers
    HTTP 500 with a ``Dive failed`` message for a missing file or column; that is
    retried like any server error and then reported as unreachable.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 3,
        retry_delay: float = 2.0,
        interval: float = 1.0,
    ) -> None:
        self._session = session
        self._owned = session is None
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._interval = interval
        self._last = 0.0
        self.request_count = 0
        logger.info(
            "Client ready: %d attempts per request, %.0fs base retry delay, "
            "at most one request per %.1fs",
            attempts,
            retry_delay,
            interval,
        )

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            logger.debug("Opening HTTP session (timeout %ss)", TIMEOUT.total)
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
        return self._session

    async def close(self) -> None:
        if self._owned and self._session is not None:
            logger.info("Closing HTTP session after %d requests", self.request_count)
            await self._session.close()

    async def get_json(self, url: str, params: Params = None) -> Any:
        """Return the decoded JSON body of ``url``."""
        return await self._get(url, params, encoding=None, json=True)

    async def get_text(
        self, url: str, params: Params = None, *, encoding: str | None = None
    ) -> str:
        """Return the text body of ``url`` decoded with ``encoding``."""
        return await self._get(url, params, encoding=encoding, json=False)

    async def _pace(self) -> None:
        wait = self._last + self._interval - time.monotonic()
        if wait > 0:
            logger.debug("Pacing: waiting %.2fs before the next request", wait)
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    async def _get(self, url: str, params: Params, *, encoding, json: bool):
        query = list(params or [])
        for attempt in range(1, self._attempts + 1):
            await self._pace()
            started = time.monotonic()
            self.request_count += 1
            retry_after = 0.0
            try:
                async with self.session.get(url, params=query) as response:
                    if response.status == 429 or response.status >= 500:
                        retry_after = _retry_after(response.headers.get("Retry-After"))
                        detail = (await response.text())[:300]
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"{response.reason}: {detail}",
                        )
                    response.raise_for_status()
                    if json:
                        body = await response.json(content_type=None)
                    else:
                        body = await response.text(encoding=encoding)
                    location = response.url
            except (aiohttp.ClientError, TimeoutError) as error:
                transient = not isinstance(error, aiohttp.ClientResponseError) or (
                    error.status == 429 or error.status >= 500
                )
                if not transient or attempt == self._attempts:
                    logger.error(
                        "Giving up on %s after %d attempt(s): %s (%s)",
                        url,
                        attempt,
                        type(error).__name__,
                        "retries exhausted" if transient else "not retryable",
                    )
                    raise Unreachable(
                        f"GET {url} {query} failed: {type(error).__name__}: {error}"
                    ) from error
                delay = max(self._retry_delay * 2 ** (attempt - 1), retry_after)
                logger.warning(
                    "Attempt %d/%d for %s failed (%s: %s); retrying in %.0fs",
                    attempt,
                    self._attempts,
                    url,
                    type(error).__name__,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.debug(
                "GET %s (%.1fs, request %d)",
                location,
                time.monotonic() - started,
                self.request_count,
            )
            return body
        raise AssertionError("unreachable")


def _retry_after(value: str | None) -> float:
    try:
        return min(float(value), 300.0) if value else 0.0
    except ValueError:
        return 0.0

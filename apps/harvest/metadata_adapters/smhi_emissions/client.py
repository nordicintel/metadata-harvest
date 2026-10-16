"""Sequential, paced, retrying HTTP client for harvesting from SMHI's emissions API."""

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

from harvest.lifecycle import Unreachable as _Unreachable
from retrieval.data_adapters.smhi_emissions import (
    API_ROOT,
    DATA_URL,
    FILTER_OPTIONS_URL,
    NAMESPACE,
    NATIONAL,
    PROVIDER_CODE,
    TOTAL,
    data_parameters,
    dataset_code,
)

logger = logging.getLogger("smhi_emissions_logger")

TIMEOUT = aiohttp.ClientTimeout(total=60)
USER_AGENT = "nordicintel-metadata-harvest/smhi_emissions"


__all__ = [
    "API_ROOT",
    "DATA_URL",
    "DOC_URL",
    "FILTER_OPTIONS_URL",
    "NAMESPACE",
    "NATIONAL",
    "PROVIDER_CODE",
    "SOURCE_URL",
    "SmhiClient",
    "TOTAL",
    "Unreachable",
    "data_parameters",
    "dataset_code",
    "probe_parameters",
]

SOURCE_URL = "https://nationellaemissionsdatabasen.smhi.se/"
"""The public map and chart application; there is no page per substance."""
DOC_URL = "https://www.smhi.se/data/luftkvalitet/nationella-emissionsdatabasen"
"""SMHI's information page (method, user manual, contact address)."""


def probe_parameters(substance: str) -> dict[str, str]:
    """The national ``hsektor=0`` query: Totalt and main sectors for every year."""
    return data_parameters(NATIONAL, TOTAL, substance)


class Unreachable(_Unreachable):
    """SMHI did not answer a request, after every retry."""


class SmhiClient:
    """Fetch JSON one request at a time through one session.

    Request starts are at least ``interval`` seconds apart (one per second by
    default). Timeouts, connection errors, HTTP 429 and 5xx responses are retried
    with exponential backoff, honouring ``Retry-After``; exhausted retries and other
    HTTP errors raise :class:`Unreachable`.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 4,
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

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
        return self._session

    async def close(self) -> None:
        if self._owned and self._session is not None:
            await self._session.close()

    async def _pace(self) -> None:
        wait = self._last + self._interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last = time.monotonic()

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        """Return the decoded JSON body of ``url``."""
        for attempt in range(1, self._attempts + 1):
            await self._pace()
            started = time.monotonic()
            self.request_count += 1
            retry_after = None
            try:
                async with self.session.get(url, params=params) as response:
                    retry_after = response.headers.get("Retry-After")
                    response.raise_for_status()
                    body = await response.json(content_type=None)
                    location = response.url
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                if isinstance(error, aiohttp.ClientResponseError):
                    transient = error.status == 429 or error.status >= 500
                else:  # transport errors retry; an undecodable body does not
                    transient = not isinstance(error, ValueError)
                if not transient or attempt == self._attempts:
                    logger.warning(
                        "GET %s failed after %d of %d attempts (%s: %s)%s",
                        url,
                        attempt,
                        self._attempts,
                        type(error).__name__,
                        error,
                        "" if transient else "; not retryable",
                    )
                    raise Unreachable(
                        f"GET {url} {dict(params or {})} failed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                delay = self._retry_delay * 2 ** (attempt - 1)
                if retry_after and retry_after.isdigit():
                    if float(retry_after) > delay:
                        logger.info("Honouring Retry-After of %ss", retry_after)
                    delay = max(delay, float(retry_after))
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
            logger.debug("GET %s (%.1fs)", location, time.monotonic() - started)
            return body
        raise AssertionError("unreachable")

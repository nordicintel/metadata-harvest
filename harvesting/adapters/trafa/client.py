"""Sequential, retrying HTTP client for harvesting from Trafikanalys."""

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

from harvesting.adapters.trafa.api import ACCEPT_JSON

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=60)
USER_AGENT = "nordicintel-metadata-harvest/trafa"


class Unreachable(Exception):
    """Trafikanalys did not answer a request, after every retry."""


class TrafaClient:
    """Fetch Trafikanalys resources one at a time through one session.

    Every request sends ``Accept: application/json``. Timeouts, connection errors,
    HTTP 429 and 5xx responses are retried with exponential backoff; exhausted
    retries and other HTTP errors raise :class:`Unreachable`.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 4,
        retry_delay: float = 2.0,
    ) -> None:
        self._session = session
        self._owned = session is None
        self._attempts = attempts
        self._retry_delay = retry_delay
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

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        """Return the decoded JSON body of ``url``."""
        return await self._get(url, params, json=True)

    async def get_text(self, url: str, params: Mapping[str, str] | None = None) -> str:
        """Return the text body of ``url``, such as an HTML documentation page."""
        return await self._get(url, params, json=False)

    async def _get(self, url: str, params: Mapping[str, str] | None, *, json: bool):
        for attempt in range(1, self._attempts + 1):
            started = time.monotonic()
            self.request_count += 1
            try:
                async with self.session.get(
                    url, params=params, headers=ACCEPT_JSON
                ) as response:
                    response.raise_for_status()
                    if json:
                        body = await response.json(content_type=None)
                    else:
                        body = await response.text()
                    location = response.url
            except (aiohttp.ClientError, TimeoutError) as error:
                transient = not isinstance(error, aiohttp.ClientResponseError) or (
                    error.status == 429 or error.status >= 500
                )
                if not transient or attempt == self._attempts:
                    raise Unreachable(
                        f"GET {url} {dict(params or {})} failed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                delay = self._retry_delay * 2 ** (attempt - 1)
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
            logger.info("GET %s (%.1fs)", location, time.monotonic() - started)
            return body
        raise AssertionError("unreachable")

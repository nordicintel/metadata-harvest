"""Sequential, paced and retrying HTTP client for harvesting from the Riksbank.

Anonymous clients get HTTP 429 with ``Retry-After`` (45–56 s observed) after about
five quick calls, so the client waits ``request_interval_seconds`` (default 15)
between requests and honours ``Retry-After``.
"""

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import aiohttp

from metadata_harvest.adapters.riksbank.api import ACCEPT_JSON

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=120)
USER_AGENT = "nordicintel-metadata-harvest/riksbank"
KEY_VARIABLE = "RIKSBANK_SUBSCRIPTION_KEY"
"""Optional environment variable with a Riksbank API portal subscription key."""
KEY_HEADER = "Ocp-Apim-Subscription-Key"
REQUEST_INTERVAL = 15.0
MAX_RETRY_AFTER = 180.0
"""Longest ``Retry-After`` honoured; a longer one counts as unreachable."""


class Unreachable(Exception):
    """The Riksbank did not answer a request, after every retry."""


def _retry_after(response_headers: Mapping[str, str] | None) -> float | None:
    try:
        return float((response_headers or {}).get("Retry-After", ""))
    except ValueError:
        return None


class RiksbankClient:
    """Fetch Riksbank resources one at a time through one session.

    Requests are spaced at least ``interval`` seconds apart. Timeouts, connection
    errors, HTTP 429 and 5xx responses are retried, waiting ``Retry-After`` when
    given and exponential backoff otherwise; exhausted retries and other HTTP
    errors raise :class:`Unreachable`. HTTP 204 (and 404 where allowed) return None.
    """

    def __init__(
        self,
        provider: Mapping[str, Any] | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 4,
        retry_delay: float = 15.0,
        interval: float | None = None,
    ) -> None:
        provider = provider or {}
        self._session = session
        self._owned = session is None
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._interval = float(
            interval
            if interval is not None
            else provider.get("request_interval_seconds", REQUEST_INTERVAL)
        )
        self._last: float | None = None
        self._headers = dict(ACCEPT_JSON)
        if key := os.environ.get(KEY_VARIABLE, "").strip():
            self._headers[KEY_HEADER] = key  # never logged
        self.request_count = 0

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
        return self._session

    @property
    def keyed(self) -> bool:
        """Whether requests carry a subscription key."""
        return KEY_HEADER in self._headers

    async def close(self) -> None:
        if self._owned and self._session is not None:
            await self._session.close()

    async def pace(self) -> None:
        """Wait until the next request may be sent, and claim that slot."""
        if self._last is not None:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last = time.monotonic()

    async def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        *,
        missing_ok: bool = False,
    ) -> Any:
        """Return the decoded JSON body of ``url``, or None for HTTP 204.

        With ``missing_ok``, HTTP 404 also returns None (the forecast API answers
        404 for a policy round without a vintage).
        """
        for attempt in range(1, self._attempts + 1):
            await self.pace()
            started = time.monotonic()
            self.request_count += 1
            wait = None
            try:
                async with self.session.get(
                    url, params=params, headers=self._headers
                ) as response:
                    if response.status == 204 or (
                        missing_ok and response.status == 404
                    ):
                        logger.info("GET %s: HTTP %d", response.url, response.status)
                        return None
                    if response.status == 429 or response.status >= 500:
                        wait = _retry_after(response.headers)
                    response.raise_for_status()
                    body = await response.json(content_type=None)
                    location = response.url
            except (aiohttp.ClientError, TimeoutError) as error:
                transient = not isinstance(error, aiohttp.ClientResponseError) or (
                    error.status == 429 or error.status >= 500
                )
                if wait is not None and wait > MAX_RETRY_AFTER:
                    transient = False
                if not transient or attempt == self._attempts:
                    raise Unreachable(
                        f"GET {url} {dict(params or {})} failed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                delay = (
                    wait if wait is not None else self._retry_delay * 2 ** (attempt - 1)
                )
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

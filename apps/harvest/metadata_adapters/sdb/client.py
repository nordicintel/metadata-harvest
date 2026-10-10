"""Polite, retrying JSON client for harvesting from the SDB API."""

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import aiohttp

from retrieval.data_adapters.sdb import API_ROOT

logger = logging.getLogger("sdb_logger")

TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=30, sock_read=120)
USER_AGENT = "nordicintel-metadata-harvest/sdb"


class Unreachable(Exception):
    """SDB did not serve a resource retrieval depends on, after every retry."""


class NotFoundError(LookupError):
    """SDB answered 404 for a metadata request."""


class SdbClient:
    """Fetch SDB metadata resources sequentially through one session.

    Timeouts, connection errors, HTTP 429 and 5xx responses are retried with
    exponential backoff; other HTTP errors and exhausted retries raise
    :class:`Unreachable`. HTTP 404 raises :class:`NotFoundError`.
    """

    def __init__(
        self,
        provider: dict | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self._api_root = (provider or {}).get("base_url", API_ROOT).rstrip("/")
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
        logger.info(
            "SDB client done: %d requests (including retries) to %s",
            self.request_count,
            self._api_root,
        )
        if self._owned and self._session is not None:
            await self._session.close()

    def url(self, *parts: str) -> str:
        """Return the URL for path ``parts`` below the API root."""
        return "/".join([self._api_root, *(quote(part, safe="") for part in parts)])

    async def get(self, *parts: str, params: Mapping[str, int] | None = None) -> Any:
        """Return the decoded JSON at ``parts``, retrying transient failures."""
        url = self.url(*parts)
        for attempt in range(1, self._attempts + 1):
            started = time.monotonic()
            try:
                self.request_count += 1
                async with self.session.get(
                    url, params=params, headers={"Accept": "application/json"}
                ) as response:
                    if response.status == 404:
                        logger.debug("GET %s answered 404", url)
                        raise NotFoundError(f"SDB returned 404 for {url}")
                    response.raise_for_status()
                    body = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as error:
                transient = not isinstance(error, aiohttp.ClientResponseError) or (
                    error.status == 429 or error.status >= 500
                )
                if not transient or attempt == self._attempts:
                    logger.warning(
                        "Giving up on %s after %d/%d attempts (%s): %s",
                        url,
                        attempt,
                        self._attempts,
                        "permanent error" if not transient else "retries exhausted",
                        error,
                    )
                    raise Unreachable(
                        f"GET {url} {dict(params or {})} failed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                delay = self._retry_delay * 2 ** (attempt - 1)
                logger.warning(
                    "Attempt %d/%d for %s failed (%s); backing off %.0fs before "
                    "retrying",
                    attempt,
                    self._attempts,
                    url,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.debug(
                "GET %s %s (%.1fs, attempt %d)",
                url,
                dict(params or {}),
                time.monotonic() - started,
                attempt,
            )
            return body
        raise AssertionError("unreachable")

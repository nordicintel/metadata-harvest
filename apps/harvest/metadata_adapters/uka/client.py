"""Retrying UKÄ client for harvesting; exports are downloaded, never cached."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from apps.harvest.metadata_adapters.uka.source import CATALOGUE_URL, totals_url
from retrieval._uka_export import API_ROOT, EXPORT_PARAMS, NO_PERIOD_ERROR

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=120)
USER_AGENT = "nordicintel-metadata-harvest/uka"


class Unreachable(Exception):
    """UKÄ did not serve a resource retrieval depends on, after every retry."""


@dataclass(frozen=True)
class ExportFile:
    url: str
    text: str
    last_modified: str | None


class UkaClient:
    """Fetch UKÄ resources through one session with bounded concurrency.

    Timeouts, connection errors, HTTP 429 and 5xx responses are retried with
    exponential backoff; other HTTP errors raise. HTTP 404 answers None.
    """

    def __init__(
        self,
        provider: dict | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 4,
        retry_delay: float = 2.0,
    ) -> None:
        provider = provider or {}
        self.base_url = provider.get("base_url", API_ROOT)
        self._session = session
        self._owned = session is None
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._limit = asyncio.Semaphore(int(provider.get("concurrency", 2)))
        self.request_count = 0
        self.downloaded_bytes = 0

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

    def _url(self, url: str) -> str:
        return url.replace(API_ROOT, self.base_url, 1)

    async def catalogue(self) -> Any:
        body = await self.get_json(CATALOGUE_URL)
        if body is None:
            raise Unreachable(f"{CATALOGUE_URL} answered HTTP 404")
        return body

    async def totals(self, indicator_id: int) -> Any:
        body = await self.get_json(totals_url(indicator_id))
        if body is None:
            raise Unreachable(f"{totals_url(indicator_id)} answered HTTP 404")
        return body

    async def export_link(self, indicator_id: int, period: str) -> str | None:
        """The file URL of a period's export, or None when UKÄ has no export."""
        url = totals_url(indicator_id)
        body = await self.get_json(url, {**EXPORT_PARAMS, "year": period})
        if body is None:
            raise Unreachable(f"{url} export link for {period} answered HTTP 404")
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, str) and error.startswith(NO_PERIOD_ERROR):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            raise Unreachable(f"{url}: no export link for {period}: {str(body)[:200]}")
        return body["url"]

    async def export(self, url: str) -> ExportFile | None:
        """Download an export file; None for HTTP 404."""

        async def read(response: aiohttp.ClientResponse) -> ExportFile | None:
            if response.status == 404:
                return None
            response.raise_for_status()
            content = await response.read()
            self.downloaded_bytes += len(content)
            return ExportFile(
                url,
                content.decode("utf-8-sig"),
                response.headers.get("Last-Modified"),
            )

        return await self._request("GET", url, None, read)

    async def available(self, url: str) -> bool:
        """Whether an export file answers HEAD with success (False for HTTP 404)."""

        async def read(response: aiohttp.ClientResponse) -> bool:
            if response.status == 404:
                return False
            response.raise_for_status()
            return True

        return await self._request("HEAD", url, None, read)

    async def get_json(self, url: str, params: dict | None = None) -> Any | None:
        """The decoded JSON body of ``url``, or None for HTTP 404."""

        async def read(response: aiohttp.ClientResponse) -> Any | None:
            if response.status == 404:
                return None
            response.raise_for_status()
            return await response.json(content_type=None)

        return await self._request("GET", url, params, read)

    async def _request(self, method, url, params, read):
        for attempt in range(1, self._attempts + 1):
            started = time.monotonic()
            self.request_count += 1
            try:
                async with self._limit:
                    async with self.session.request(
                        method, self._url(url), params=params
                    ) as response:
                        if response.status == 429 or response.status >= 500:
                            response.raise_for_status()
                        result = await read(response)
                        status = response.status
            except (aiohttp.ClientError, TimeoutError) as error:
                transient = not isinstance(error, aiohttp.ClientResponseError) or (
                    error.status == 429 or error.status >= 500
                )
                if not transient or attempt == self._attempts:
                    raise Unreachable(
                        f"{method} {url} {params or ''} failed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                delay = self._retry_delay * 2 ** (attempt - 1)
                logger.warning(
                    "Attempt %d/%d for %s failed (%s); retrying in %.0fs",
                    attempt,
                    self._attempts,
                    url,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.info(
                "%s %s -> %d (%.1fs)", method, url, status, time.monotonic() - started
            )
            return result
        raise AssertionError("unreachable")

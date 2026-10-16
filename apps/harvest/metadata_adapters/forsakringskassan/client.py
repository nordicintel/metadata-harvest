"""Sequential, paced and retrying HTTP client for harvesting from Försäkringskassan."""

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from harvest.lifecycle import Unreachable as _Unreachable
from retrieval.data_adapters.forsakringskassan import (
    API_ROOT,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    ROW_COUNTER,
    SUPPRESSED,
    meta_url,
    table_url,
)

logger = logging.getLogger("forsakringskassan_logger")

TIMEOUT = aiohttp.ClientTimeout(total=120)
USER_AGENT = "nordicintel-metadata-harvest/forsakringskassan"
MIN_INTERVAL = 1.0
"""Seconds between the starts of two requests: at most one request per second."""
MAX_RETRY_AFTER = 300.0

__all__ = [
    "API_ROOT",
    "MEASURE_DIMENSION",
    "PROVIDER_CODE",
    "REPORTS_URL",
    "ROW_COUNTER",
    "SITE",
    "SUPPRESSED",
    "TEXTS_URL",
    "meta_url",
    "source_url",
    "table_url",
]

SITE = "https://www.forsakringskassan.se"
REPORTS_URL = f"{API_ROOT}/reports"
"""The catalogue: benefit groups (``key``) in areas, each with its report keys."""
TEXTS_URL = f"{SITE}/rest-api/fk-rest-textnycklar/public/v2/meka?lang=sv-se"
"""Swedish text keys of the statistics database: report names, ingresses and
descriptions (``MEKA.report.{report}``), benefits (``MEKA.forman.{forman}``) and
areas (``MEKA.area.{area}``). ``lang=en`` answers the same Swedish texts."""
STATISTICS_PAGE = f"{SITE}/statistik-och-analys/statistikdatabas"


def source_url(area: str, report: str) -> str:
    """Return the report's page in the web statistics database."""
    return f"{STATISTICS_PAGE}#!/{quote(area, safe='')}/{quote(report, safe='')}"


class Unreachable(_Unreachable):
    """Försäkringskassan did not answer a request, after every retry."""


def _retry_after(error: aiohttp.ClientResponseError) -> float | None:
    value = (error.headers or {}).get("Retry-After")
    try:
        return min(float(value), MAX_RETRY_AFTER) if value is not None else None
    except ValueError:
        return None


class ForsakringskassanClient:
    """Fetch Försäkringskassan resources one at a time through one session.

    Requests start at least ``min_interval`` seconds apart. Timeouts, connection
    errors, HTTP 429 and 5xx responses are retried with exponential backoff, or
    after ``Retry-After`` when the response gives one; exhausted retries and other
    HTTP errors (such as 404) raise :class:`Unreachable`. The session cookie the
    site sets is not needed and not relied on.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 4,
        retry_delay: float = 2.0,
        min_interval: float = MIN_INTERVAL,
    ) -> None:
        self._session = session
        self._owned = session is None
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._min_interval = min_interval
        self._last_start: float | None = None
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
        if self._last_start is not None:
            wait = self._min_interval - (time.monotonic() - self._last_start)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_start = time.monotonic()

    async def get_json(self, url: str) -> Any:
        """Return the decoded JSON body of ``url``."""
        for attempt in range(1, self._attempts + 1):
            await self._pace()
            started = time.monotonic()
            self.request_count += 1
            try:
                async with self.session.get(
                    url, headers={"Accept": "application/json"}
                ) as response:
                    response.raise_for_status()
                    body = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                transient = not isinstance(
                    error, aiohttp.ClientResponseError | ValueError
                ) or (
                    isinstance(error, aiohttp.ClientResponseError)
                    and (error.status == 429 or error.status >= 500)
                )
                if not transient or attempt == self._attempts:
                    logger.warning(
                        "Giving up on %s after attempt %d/%d (%s: %s)%s",
                        url,
                        attempt,
                        self._attempts,
                        type(error).__name__,
                        error,
                        "" if transient else "; not retryable",
                    )
                    raise Unreachable(
                        f"GET {url} failed: {type(error).__name__}: {error}"
                    ) from error
                delay = self._retry_delay * 2 ** (attempt - 1)
                if isinstance(error, aiohttp.ClientResponseError):
                    delay = max(delay, _retry_after(error) or 0)
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
                "GET %s ok in %.1fs (request %d)",
                url,
                time.monotonic() - started,
                self.request_count,
            )
            return body
        raise AssertionError("unreachable")

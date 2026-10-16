"""Paced HTTP clients for EntryScape instances, counting requests.

Each instance gets its own ``Pacer``. An HTTP 429 doubles its interval; every
successful request then brings it back towards the base interval, so one burst
of throttling does not slow the rest of the run.
"""

import asyncio
import json
import logging
import time
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger("entryscape_logger")

# Apache on EntryScape instances answers HTTP 414 for request URLs around 9.5 KB
# (8 KB worked). Stay well below that.
MAX_URL_LENGTH = 6000
MAX_INTERVAL = 5.0
ATTEMPTS = 6
MIN_THROTTLE_PAUSE = 30.0  # seconds to wait after an HTTP 429, at least


class RateLimited(RuntimeError):
    """The host throttled us again after we had already slowed down: stop."""


class BudgetExhausted(RuntimeError):
    """The run's request budget is used up."""


class Pacer:
    """Spaces requests ``interval`` seconds apart and enforces the run's limits.

    - ``budget``: hard cap on requests; the next request after it raises
      ``BudgetExhausted``.
    - ``max_throttles``: HTTP 429 responses tolerated. The first pauses every
      client sharing this pacer for at least ``MIN_THROTTLE_PAUSE`` seconds and
      doubles the interval; one more raises ``RateLimited``.
    """

    def __init__(
        self,
        interval: float = 0.25,
        *,
        budget: int | None = None,
        max_throttles: int | None = None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.base = self.interval = interval
        self.budget, self.max_throttles = budget, max_throttles
        self.clock, self.sleep = clock, sleep
        self.next_request = 0.0
        self.lock = asyncio.Lock()
        self.sent = 0
        self.throttles = 0

    async def wait(self) -> None:
        async with self.lock:
            if self.budget is not None and self.sent >= self.budget:
                logger.warning(
                    "Request budget of %d used up; refusing further requests",
                    self.budget,
                )
                raise BudgetExhausted(f"request budget of {self.budget} used up")
            self.sent += 1
            if self.sent % 100 == 0:
                logger.info(
                    "%d requests sent (budget %s, interval %.2fs)",
                    self.sent,
                    self.budget if self.budget is not None else "unlimited",
                    self.interval,
                )
            delay = max(0.0, self.next_request - self.clock())
            self.next_request = max(self.next_request, self.clock()) + self.interval
        await self.sleep(delay)

    def slow_down(self, retry_after: float) -> None:
        self.throttles += 1
        if self.max_throttles is not None and self.throttles > self.max_throttles:
            logger.error(
                "HTTP 429 received %d times, only %d tolerated; stopping the run",
                self.throttles,
                self.max_throttles,
            )
            raise RateLimited(f"HTTP 429 received {self.throttles} times")
        self.interval = min(MAX_INTERVAL, max(self.interval, 0.05) * 2)
        pause = max(
            retry_after,
            MIN_THROTTLE_PAUSE if self.max_throttles is not None else retry_after,
        )
        self.next_request = max(self.next_request, self.clock() + pause)
        logger.warning(
            "HTTP 429 (throttle %d): pausing all requests for %.0fs, "
            "request interval raised to %.2fs",
            self.throttles,
            pause,
            self.interval,
        )

    def recover(self) -> None:
        self.interval = max(self.base, self.interval * 0.9)


class Client:
    """Async GET-only client with retries, shared pacing and a request counter."""

    def __init__(
        self,
        base_url: str,
        *,
        pacer: Pacer | None = None,
        interval: float = 0.25,
        transport=None,
    ):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=60,
            transport=transport,
            headers={"User-Agent": "nordicintel-metadata-harvest/entryscape"},
        )
        self.pacer = pacer or Pacer(interval)
        self.requests = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    def url_length(self, path: str, params: dict) -> int:
        """Length of the request URL, computed without building it.

        httpx refuses to build URLs with very long query components, so measuring
        through it fails exactly when the answer matters.
        """
        base = str(self.http.base_url.join(path))
        return len(base) + 1 + len(urlencode(params, quote_via=quote))

    async def get(self, path: str, params: dict | None = None) -> dict:
        """Get a JSON object; retry transport errors, 429 and 5xx with back-off.

        Sends ``Accept: application/json``: EntryStore metadata endpoints answer
        RDF/XML without it.
        """
        response = await self._get(path, params or {}, {"Accept": "application/json"})
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object from {path}")
        json.dumps(payload, allow_nan=False)
        return payload

    async def get_bytes(self, path: str) -> tuple[str, bytes]:
        """Download a file; return its content type and body."""
        response = await self._get(path, {}, {})
        return response.headers.get("content-type", ""), response.content

    async def _get(self, path: str, params: dict, headers: dict) -> httpx.Response:
        if self.url_length(path, params) > MAX_URL_LENGTH:
            raise ValueError(f"Request URL exceeds {MAX_URL_LENGTH} characters")
        for attempt in range(ATTEMPTS):
            await self.pacer.wait()
            self.requests += 1
            logger.debug(
                "GET %s params=%s (attempt %d/%d)",
                path,
                sorted(params),
                attempt + 1,
                ATTEMPTS,
            )
            try:
                response = await self.http.get(path, params=params, headers=headers)
                response.raise_for_status()
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                response = (
                    error.response if isinstance(error, httpx.HTTPStatusError) else None
                )
                retryable = (
                    response is None
                    or response.status_code == 429
                    or response.status_code >= 500
                )
                outcome = (
                    f"HTTP {response.status_code}"
                    if response is not None
                    else type(error).__name__
                )
                if attempt == ATTEMPTS - 1 or not retryable:
                    if retryable:
                        logger.warning(
                            "Giving up on %s after %d attempts: %s",
                            path,
                            attempt + 1,
                            outcome,
                        )
                    else:
                        logger.debug("%s answered %s; not retrying", path, outcome)
                    raise
                delay = 2.0**attempt
                header = (
                    response.headers.get("Retry-After", "")
                    if response is not None
                    else ""
                )
                if header.isdigit():
                    delay = max(delay, float(header))
                if delay > 120:
                    logger.warning(
                        "Giving up on %s: server asks for a %.0fs wait (%s)",
                        path,
                        delay,
                        outcome,
                    )
                    raise
                logger.warning(
                    "Retrying %s after %s (attempt %d/%d)",
                    path,
                    outcome,
                    attempt + 1,
                    ATTEMPTS,
                )
                if response is not None and response.status_code == 429:
                    self.pacer.slow_down(delay)
                else:
                    await asyncio.sleep(delay)
                continue
            self.pacer.recover()
            return response
        raise RuntimeError("Request did not produce a result")

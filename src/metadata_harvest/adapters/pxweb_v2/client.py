"""One paced HTTP connection pool for a provider, shared across both languages."""

import asyncio
import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

SCB_URL = "https://statistikdatabasen.scb.se/api/v2"


class Client:
    """Async GET-only client with bounded retries and source-aware pacing."""

    def __init__(
        self,
        base_url: str = SCB_URL,
        *,
        transport=None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=60,
            transport=transport,
            headers={"User-Agent": "metadata-harvest/0.1 SCB metadata"},
        )
        self.interval = 0.5
        self.clock, self.sleep = clock, sleep
        self.next_request = 0.0
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    def configure(self, config: dict) -> None:
        calls, window = config["maxCallsPerTimeWindow"], config["timeWindow"]
        if (
            not isinstance(calls, (int, float))
            or not isinstance(window, (int, float))
            or calls <= 0
            or window <= 0
        ):
            raise ValueError("Invalid API rate limit configuration")
        previous = self.interval
        self.interval = max(0.5, window / calls)
        self.next_request += max(0, self.interval - previous)

    async def get(self, path: str, **params) -> dict:
        """Get a JSON object; retry transient failures at most three times."""
        async with self.lock:
            for attempt in range(4):
                await self.sleep(max(0, self.next_request - self.clock()))
                self.next_request = self.clock() + self.interval
                try:
                    response = await self.http.get(path, params=params)
                    response.raise_for_status()
                except (httpx.TransportError, httpx.HTTPStatusError) as error:
                    response = (
                        error.response
                        if isinstance(error, httpx.HTTPStatusError)
                        else None
                    )
                    if attempt == 3 or (
                        response is not None
                        and response.status_code != 429
                        and response.status_code < 500
                    ):
                        raise
                    delay = 2**attempt
                    if response is not None and response.headers.get("Retry-After"):
                        raw = response.headers["Retry-After"]
                        try:
                            delay = max(delay, float(raw))
                        except ValueError:
                            delay = max(
                                delay,
                                (
                                    parsedate_to_datetime(raw) - datetime.now(UTC)
                                ).total_seconds(),
                            )
                    if delay > 120:
                        raise
                    self.next_request = max(self.next_request, self.clock() + delay)
                    continue
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"Expected a JSON object from {path}")
                # Python's decoder accepts NaN/Infinity; reject them at the source
                # boundary so one bad response cannot poison a grouped write.
                json.dumps(payload, allow_nan=False)
                return payload
        raise RuntimeError("Request did not produce a result")

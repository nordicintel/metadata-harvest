"""The harvest client against a local server: pacing, retries, rejections."""

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from metadata_harvest.adapters.dataanalys.client import (
    DataanalysClient,
    RequestRejected,
    Unreachable,
)


def serve(answers, scenario):
    """Run ``scenario(client, url, seen)`` against a server replaying ``answers``."""
    seen = []

    async def handle(request):
        seen.append((request.method, time.monotonic(), await request.text()))
        status, headers, body = answers[min(len(seen), len(answers)) - 1]
        return web.json_response(body, status=status, headers=headers)

    async def main():
        app = web.Application()
        app.router.add_route("*", "/api", handle)
        async with TestServer(app) as server:
            client = DataanalysClient(interval=0.0, retry_delay=0.01)
            try:
                return await scenario(client, str(server.make_url("/api")), seen)
            finally:
                await client.close()

    return asyncio.run(main()), seen


def test_transient_errors_are_retried_honouring_retry_after():
    async def scenario(client, url, seen):
        return await client.post_json(url, {"a": 1})

    body, seen = serve(
        [(503, {}, {}), (429, {"Retry-After": "0.2"}, {}), (200, {}, {"ok": True})],
        scenario,
    )

    assert body == {"ok": True}
    assert [method for method, _, _ in seen] == ["POST"] * 3
    assert seen[2][1] - seen[1][1] >= 0.19
    assert seen[0][2] == '{"a": 1}'


def test_exhausted_retries_are_unreachable():
    async def scenario(client, url, seen):
        return await client.get_json(url, {"q": "x"})

    with pytest.raises(Unreachable, match="HTTP 502"):
        serve([(502, {}, {})], scenario)


def test_client_errors_are_rejected_with_the_message():
    async def scenario(client, url, seen):
        with pytest.raises(RequestRejected) as caught:
            await client.post_json(url, {})
        return caught.value

    error, seen = serve(
        [(400, {}, {"message": "Error: Columns missing in dataset: ['Unit']"})],
        scenario,
    )

    assert len(seen) == 1
    assert error.status == 400 and "Columns missing" in error.text


def test_requests_are_paced():
    async def scenario(client, url, seen):
        client._interval = 0.15
        for _ in range(3):
            await client.get_json(url)
        return client.request_count

    count, seen = serve([(200, {}, {})], scenario)

    assert count == 3
    assert all(b[1] - a[1] >= 0.14 for a, b in zip(seen, seen[1:], strict=False))


def test_interval_comes_from_the_provider():
    assert DataanalysClient({"request_interval_seconds": 2.5})._interval == 2.5
    assert DataanalysClient({})._interval == 1.0

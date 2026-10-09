"""The harvesting client's pacing, Retry-After handling and subscription key."""

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from metadata_harvest.adapters.riksbank.client import (
    KEY_HEADER,
    KEY_VARIABLE,
    RiksbankClient,
    Unreachable,
)


class Script:
    """Answer each request with the next scripted (status, headers, body)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen: list[tuple[float, dict]] = []

    async def handle(self, request):
        self.seen.append((time.monotonic(), dict(request.headers)))
        status, headers, body = self.answers.pop(0)
        if body is None:
            return web.Response(status=status, headers=headers)
        return web.json_response(body, status=status, headers=headers)


def call(script, *, interval=0.0, attempts=3, missing_ok=False, calls=1):
    async def scenario():
        app = web.Application()
        app.router.add_get("/x", script.handle)
        async with TestServer(app) as server:
            client = RiksbankClient(
                {"request_interval_seconds": interval},
                attempts=attempts,
                retry_delay=0.0,
            )
            try:
                return [
                    await client.get_json(
                        str(server.make_url("/x")), missing_ok=missing_ok
                    )
                    for _ in range(calls)
                ], client.request_count
            finally:
                await client.close()

    return asyncio.run(scenario())


def test_retry_after_is_honoured():
    script = Script((429, {"Retry-After": "1"}, {"statusCode": 429}), (200, {}, [1]))

    (body,), count = call(script)

    assert body == [1] and count == 2
    assert script.seen[1][0] - script.seen[0][0] >= 0.9


def test_requests_are_paced():
    script = Script((200, {}, [1]), (200, {}, [2]))

    bodies, _ = call(script, interval=0.5, calls=2)

    assert bodies == [[1], [2]]
    assert script.seen[1][0] - script.seen[0][0] >= 0.45


def test_exhausted_retries_are_unreachable():
    script = Script(*[(429, {"Retry-After": "0"}, {"statusCode": 429})] * 3)

    with pytest.raises(Unreachable, match="429"):
        call(script)


def test_overlong_retry_after_is_not_waited_for():
    script = Script((429, {"Retry-After": "3600"}, {"statusCode": 429}))

    with pytest.raises(Unreachable):
        call(script)
    assert len(script.seen) == 1


def test_no_content_and_allowed_not_found_are_none():
    assert call(Script((204, {}, None)))[0] == [None]
    assert call(Script((404, {}, None)), missing_ok=True)[0] == [None]
    with pytest.raises(Unreachable, match="404"):
        call(Script((404, {}, None)))


def test_subscription_key_is_sent_but_never_reported(monkeypatch):
    monkeypatch.setenv(KEY_VARIABLE, "secret-key")
    script = Script((200, {}, [1]), (403, {}, {"message": "denied"}))

    call(script)
    with pytest.raises(Unreachable) as caught:
        call(Script((403, {}, {"message": "denied"})))

    assert script.seen[0][1][KEY_HEADER] == "secret-key"
    assert "secret-key" not in str(caught.value)


def test_no_key_by_default(monkeypatch):
    monkeypatch.delenv(KEY_VARIABLE, raising=False)
    script = Script((200, {}, [1]))

    call(script)

    assert KEY_HEADER not in script.seen[0][1]

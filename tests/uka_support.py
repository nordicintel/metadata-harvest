"""Test support: recorded UKÄ responses served by a local imitation of UKÄ.

``fixtures/uka/uka.json`` and ``fixtures/uka/exports/`` were recorded by
uka-adapter ``ca533a7``: the catalogue, indicators 67 and 72 with every period,
and single periods of indicators 7, 14, 15 and 97.
"""

import asyncio
import copy
import gzip
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

FIXTURES = Path(__file__).parent / "fixtures" / "uka"
RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "uka.json").read_text(encoding="utf-8")
)
UKA = "https://statistik-api.uka.se"


def export_text(name: str) -> str:
    """The text of a recorded export file, by name (without ``.gz``)."""
    return gzip.decompress((FIXTURES / "exports" / f"{name}.gz").read_bytes()).decode(
        "utf-8-sig"
    )


def export_for(indicator: int, period: str) -> tuple[str, str, str | None]:
    """URL, text and Last-Modified of a recorded export."""
    link = RECORDED["responses"][
        f"/api/totals/{indicator}?"
        + urlencode(
            sorted({"export": "all", "export_type": "csv", "year": period}.items())
        )
    ]["url"]
    path = link.removeprefix(UKA)
    entry = RECORDED["files"][path]
    return link, export_text(entry["file"].removesuffix(".gz")), entry["last_modified"]


class FakeUka:
    """Answers requests from the recordings; unrecorded requests get HTTP 404.

    ``overrides`` maps request keys (path plus sorted query) or paths to
    ``(status, body)``; ``failures`` maps them to a number of HTTP 500 answers
    given before the recording.
    """

    def __init__(self) -> None:
        self.responses = copy.deepcopy(RECORDED["responses"])
        self.files = copy.deepcopy(RECORDED["files"])
        self.overrides: dict[str, tuple[int, Any]] = {}
        self.failures: dict[str, int] = {}
        self.requests: list[str] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        query = sorted(request.query.items())
        key = request.path + (f"?{urlencode(query)}" if query else "")
        self.requests.append(
            key if request.method == "GET" else f"{request.method} {key}"
        )
        for candidate in (key, request.path):
            if self.failures.get(candidate):
                self.failures[candidate] -= 1
                return web.Response(status=500, text="Server Error")
            if candidate in self.overrides:
                status, body = self.overrides[candidate]
                if isinstance(body, str | bytes):
                    return web.Response(status=status, body=body)
                return web.json_response(body, status=status)
        if key in self.responses:
            return web.json_response(self.responses[key])
        if request.path in self.files:
            entry = self.files[request.path]
            modified = entry["last_modified"]
            if modified and request.headers.get("If-Modified-Since") == modified:
                return web.Response(status=304)
            content = gzip.decompress(
                (FIXTURES / "exports" / entry["file"]).read_bytes()
            )
            headers = {"Last-Modified": modified} if modified else {}
            return web.Response(
                body=content, headers=headers, content_type="application/octet-stream"
            )
        return web.Response(status=404, text="Not Found")


class RoutedSession:
    """A ``ClientSession`` stand-in that sends UKÄ URLs to the local server.

    Delegates to a real session, so request code runs unchanged; ``urls`` records
    every requested URL.
    """

    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        self.session = session
        self.base = base
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method, url, **kwargs):
        url = str(url)
        self.urls.append(url)
        return self.session.request(method, url.replace(UKA, self.base), **kwargs)

    @property
    def closed(self) -> bool:
        return self.session.closed


def run_with_uka(fake: FakeUka, test: Callable[[RoutedSession], Awaitable[Any]]) -> Any:
    """Run ``test`` with a routed session against ``fake`` in a fresh event loop."""

    async def main():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", fake.handle)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                routed = RoutedSession(session, str(server.make_url("")).rstrip("/"))
                result = await test(routed)
                assert not session.closed, "the caller's session was closed"
                return result
        finally:
            await server.close()

    return asyncio.run(main())

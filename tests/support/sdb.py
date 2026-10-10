"""SDB fixtures: an in-process imitation of the SDB API and result checks.

``fixtures/sdb.json`` holds trimmed responses recorded by sdb-adapter ``344212d``.
"""

import asyncio
import copy
import json
import math
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from apps.model.validation import validate_retrieval

FIXTURES = Path("tests/fixtures/sdb")
SDB_DATA = json.loads((FIXTURES / "sdb.json").read_text(encoding="utf-8"))
MAX_SEGMENT_LENGTH = 256
MAX_PAGE_SIZE = 5000


def _not_found() -> web.Response:
    return web.Response(
        status=404, text="<html>404 - File or directory not found.</html>"
    )


class FakeSdb:
    """Imitate the SDB API from recorded, trimmed fixtures.

    Reproduces the behaviour the adapter relies on: 404 for unknown resources and
    for selections without rows, a 5000-row page cap, an HTTP 400 for path segments
    over 256 characters, and ``matt`` defaulting to 1 when not selected.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = copy.deepcopy(SDB_DATA)
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.failing_paths: set[str] = set()
        self.result_names: dict[str, dict[str, str]] = {}
        """Path prefixes answered with HTTP 500."""
        self.requests: list[str] = []

    def variables(self, language: str, subject: str) -> list[dict[str, Any]] | None:
        variables = self.data["variables"].get(language, {}).get(subject)
        if variables is None and subject in self.data["variables"]["sv"]:
            variables = [
                {**variable, "text": "", "info": ""}
                for variable in self.data["variables"]["sv"][subject]
            ]
        return variables

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(str(request.rel_url))
        if any(request.path.startswith(path) for path in self.failing_paths):
            return web.json_response({"Message": "An error has occurred."}, status=500)
        if any(
            len(segment) > MAX_SEGMENT_LENGTH
            for segment in request.raw_path.split("?")[0].split("/")
        ):
            return web.Response(status=400, text="Bad Request")
        parts = [part for part in request.path.split("/") if part]
        if parts[:2] != ["api", "v1"]:
            return _not_found()
        parts = parts[2:]
        if not parts:
            return web.json_response(self.data["languages"])
        language, *rest = parts
        if not rest:
            return web.json_response(self.data["subjects"].get(language, []))
        subject, *rest = rest
        variables = self.variables(language, subject)
        if variables is None:
            return _not_found()
        if not rest:
            return web.json_response(variables)
        if rest[0] == "resultat":
            return self._results(subject, rest[1:], request.query)
        categories = (
            self.data["categories"].get(language, {}).get(subject, {}).get(rest[0])
        )
        if categories is None:
            sv = self.data["categories"]["sv"].get(subject, {}).get(rest[0])
            if sv is None:
                return _not_found()
            categories = [{**category, "text": ""} for category in sv]
        return web.json_response(categories)

    def _results(
        self, subject: str, filters: Sequence[str], query: Mapping[str, str]
    ) -> web.Response:
        renamed = self.result_names.get(subject, {})
        names = [
            renamed.get(variable["namn"], variable["namn"])
            for variable in self.data["variables"]["sv"][subject]
        ]
        selected: dict[str, set[str]] = {}
        for name, codes in zip(filters[::2], filters[1::2], strict=False):
            if name not in names:
                return _not_found()
            selected[name] = set(codes.split(","))
        if "matt" in names and "matt" not in selected:
            selected["matt"] = {"1"}
        rows = [
            row
            for row in self.rows.get(subject, [])
            if all(
                str(row[_row_key(row, name)]) in codes
                for name, codes in selected.items()
            )
        ]
        if not rows:
            return _not_found()
        per_page = min(int(query.get("per_sida", MAX_PAGE_SIZE)), MAX_PAGE_SIZE)
        page = int(query.get("sida", 1))
        return web.json_response(
            {
                "data": rows[(page - 1) * per_page : page * per_page],
                "amne": subject,
                "nasta_sida": None,
                "foregaende_sida": None,
                "sida": page,
                "per_sida": per_page,
                "sidor": math.ceil(len(rows) / per_page),
            }
        )


def _row_key(row: Mapping[str, Any], name: str) -> str:
    return name if name in row else f"{name}Id"


class RunningSdb:
    """A FakeSdb served over HTTP on a background thread."""

    def __init__(self, fake: FakeSdb) -> None:
        self.fake = fake
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._runner: web.AppRunner | None = None
        self.api_root = ""

    def start(self) -> None:
        self._thread.start()

        async def setup() -> int:
            app = web.Application()
            app.router.add_route("GET", "/{tail:.*}", self.fake.handle)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            return self._runner.addresses[0][1]

        port = asyncio.run_coroutine_threadsafe(setup(), self._loop).result(10)
        self.api_root = f"http://127.0.0.1:{port}/api/v1"

    def stop(self) -> None:
        if self._runner is not None:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(
                10
            )
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        self._loop.close()


@pytest.fixture
def sdb() -> Iterator[RunningSdb]:
    running = RunningSdb(FakeSdb())
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture(scope="session")
def check_fragment():
    """Return a function asserting the retrieval contract's result checks."""

    def check(
        fragment: Mapping[str, Any], selection: Mapping[str, Sequence[str]]
    ) -> None:
        validate_retrieval(fragment, selection=selection)
        assert list(fragment["dimension"]) == fragment["id"]
        assert set(fragment["id"]) == set(selection)
        for code, count in zip(fragment["id"], fragment["size"], strict=True):
            index = fragment["dimension"][code]["category"]["index"]
            assert count == len(index)
            assert set(index) == set(selection[code])
        cells = math.prod(fragment["size"])
        for field in ("value", "status"):
            content = fragment.get(field)
            if isinstance(content, dict):
                assert all(int(position) < cells for position in content)
            elif isinstance(content, list):
                assert len(content) == cells

    return check

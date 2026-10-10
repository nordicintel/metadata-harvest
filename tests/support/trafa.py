"""Shared fixtures: recorded Trafikanalys responses served by a stand-in client.

``fixtures/trafa.json`` holds real responses recorded by trafa-adapter ``936b20e``
for T10011, T10033 and T04010, keyed by URL with sorted query parameters.
"""

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

FIXTURES = Path("tests/fixtures/trafa")
RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "trafa.json").read_text(encoding="utf-8")
)


def fixture_key(url: str, params: dict[str, str] | None) -> str:
    return f"{url}?{urlencode(sorted(params.items()))}" if params else url


class RecordedClient:
    """Answer harvester requests from recorded responses, like ``TrafaClient``.

    Unrecorded requests raise ``LookupError``; keys in ``failing`` raise
    ``TimeoutError`` to imitate an unreachable host.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = copy.deepcopy(RECORDED)
        self.failing: set[str] = set()
        self.requests: list[str] = []
        self.request_count = 0

    def _answer(self, url: str, params: dict[str, str] | None) -> Any:
        key = fixture_key(url, params)
        self.requests.append(key)
        self.request_count += 1
        if key in self.failing:
            raise TimeoutError(f"timed out: {key}")
        if key not in self.responses:
            raise LookupError(f"not recorded: {key}")
        return copy.deepcopy(self.responses[key])

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        return self._answer(url, params)

    async def get_text(self, url: str, params: dict[str, str] | None = None) -> str:
        return self._answer(url, params)


@pytest.fixture
def client() -> RecordedClient:
    return RecordedClient()


class Recorded:
    """Accessors for single recorded responses."""

    @staticmethod
    def structure(product: str, language: str = "sv") -> dict[str, Any]:
        return copy.deepcopy(
            RECORDED[
                fixture_key(
                    "https://api.trafa.se/api/structure",
                    {"query": product, "lang": language.upper()},
                )
            ]
        )

    @staticmethod
    def documentation(product_id: str) -> str:
        return RECORDED[
            fixture_key(
                "https://www.trafa.se/sidor/api-dokumentation/", {"code": product_id}
            )
        ]

    @staticmethod
    def data(query: str, language: str = "sv") -> dict[str, Any]:
        return copy.deepcopy(
            RECORDED[
                fixture_key(
                    "https://api.trafa.se/api/data",
                    {"query": query, "lang": language.upper()},
                )
            ]
        )


@pytest.fixture
def recorded() -> Recorded:
    return Recorded()


# --- Retrieval: an in-process imitation of Trafikanalys' /data endpoint ---------

DATA_TABLES: dict[str, Any] = json.loads(
    (FIXTURES / "data.json").read_text(encoding="utf-8")
)
"""Full ``/data`` responses recorded by ``scripts/record_fixtures.py``."""
MAX_QUERY_STRING = 2048
_PARSER_ERROR = {
    "Message": "An error has occurred.",
    "ExceptionMessage": "Object reference not set to an instance of an object.",
    "ExceptionType": "System.NullReferenceException",
    "StackTrace": "   at Trafa.Statistics.Api.Core.Misc.FromQueryStringToQuery("
    "String q, List`1 ps, List`1 ds, List`1 dvs)",
}


class FakeTrafa:
    """Answer ``/data`` queries from the recorded tables, as Trafikanalys does.

    Reproduces behaviour observed on 2026-09-25: HTTP 404 for query strings over
    2048 characters; ``Errors`` for unknown products and items; HTTP 500 from the
    query parser for unknown category codes; omitted variables answered with their
    ``t1`` rows; no rows for combinations without data.
    """

    def __init__(self) -> None:
        self.tables = copy.deepcopy(DATA_TABLES)
        self.queries: list[str] = []
        self.query_strings: list[str] = []
        self.accept: list[str | None] = []
        self.fail_status: int | None = None

    async def handle(self, request):
        from aiohttp import web

        raw = request.rel_url.raw_query_string
        self.query_strings.append(raw)
        self.accept.append(request.headers.get("Accept"))
        if len(raw) > MAX_QUERY_STRING:
            return web.Response(status=404, text="<html>404.15</html>")
        if self.fail_status is not None:
            return web.Response(status=self.fail_status, text="unavailable")
        query = request.query["query"]
        self.queries.append(query)
        product, *items = query.split("|")
        tables = [t for k, t in self.tables.items() if k.split("|")[0] == product]
        if not tables:
            return self._errors(f"Produkten '{product}' hittades inte i strukturen.")
        names = [item.split(":")[0] for item in items]
        filters = {
            item.split(":")[0]: item.split(":", 1)[1].split(",")
            for item in items
            if ":" in item
        }
        table = next((t for t in tables if set(names) <= self._columns(t).keys()), None)
        if table is None:
            known = set().union(*(self._columns(t) for t in tables))
            unknown = next(name for name in names if name not in known)
            return self._errors(f"Entiten {unknown} hittades inte i strukturen.")
        columns = self._columns(table)
        for name, codes in filters.items():
            present = {
                cell["Name"]
                for row in table["Rows"]
                for cell in row["Cell"]
                if cell["Column"] == name
            }
            if set(codes) - present:
                return web.json_response(_PARSER_ERROR, status=500)
        rows = []
        for row in table["Rows"]:
            cells = {cell["Column"]: cell for cell in row["Cell"]}
            if any(
                columns[name]["Type"] == "D"
                and name not in names
                and cells[name]["Name"] != "t1"
                for name in columns
            ):
                continue
            if any(cells[name]["Name"] not in codes for name, codes in filters.items()):
                continue
            rows.append(
                {**row, "Cell": [c for c in row["Cell"] if c["Column"] in names]}
            )
        header = [
            {**column, "Filters": filters.get(name, [])}
            for name, column in columns.items()
            if name in names
        ]
        return web.json_response(
            {
                **{k: v for k, v in table.items() if k not in ("Header", "Rows")},
                "Header": {"Column": header},
                "Rows": rows,
            }
        )

    @staticmethod
    def _columns(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {column["Name"]: column for column in table["Header"]["Column"]}

    @staticmethod
    def _errors(message: str):
        from aiohttp import web

        return web.json_response(
            {"Errors": [message], "Header": None, "Rows": None, "Notes": None}
        )


@pytest.fixture
def fake_trafa() -> FakeTrafa:
    return FakeTrafa()


async def retrieve_from(fake: FakeTrafa, selection, *, product="t10011", **kwargs):
    """Call ``retrieve`` against ``fake``; return the fragment and the session."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from retrieval.data_adapters.trafa import retrieve

    app = web.Application()
    app.router.add_get("/api/data", fake.handle)
    async with TestServer(app) as server:
        async with aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "trafa",
                "dataset_code": kwargs.pop("dataset_code", f"{product.upper()}|x"),
                "language": "sv",
                "selection": selection,
                "data_url": str(server.make_url("/api/data")),
                "metadata_url": None,
                "metadata": {
                    "/extension/trafa": {
                        "product": product,
                        "measure_dimension": "measure",
                    }
                },
                **kwargs,
            }
            fragment = await retrieve(session, **arguments)
            assert not session.closed
            return fragment


def check_fragment(fragment: dict[str, Any], selection) -> None:
    """Validate a fragment: result schema plus the contract's consumer checks."""
    from apps.model.validation import validate_retrieval

    validate_retrieval(fragment, selection=selection)
    ids = fragment["id"]
    assert ids == list(fragment["dimension"])
    assert set(ids) == set(selection)
    total = 1
    for name, size in zip(ids, fragment["size"], strict=True):
        index = fragment["dimension"][name]["category"]["index"]
        codes = index if isinstance(index, list) else sorted(index, key=index.get)
        if isinstance(index, dict):
            assert sorted(index.values()) == list(range(len(index)))
        assert size == len(codes)
        assert sorted(codes) == sorted(selection[name])
        total *= size
    for field in ("value", "status"):
        content = fragment.get(field)
        if isinstance(content, list):
            assert len(content) == total
        elif isinstance(content, dict):
            assert all(int(key) < total for key in content)


@pytest.fixture(name="retrieve_from")
def retrieve_from_fixture():
    return retrieve_from


@pytest.fixture(name="check_fragment")
def check_fragment_fixture():
    return check_fragment

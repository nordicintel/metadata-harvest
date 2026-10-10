"""Shared fixtures: recorded Tillväxtverket responses and a fake DivePort server.

``fixtures/tillvaxtverket.json`` maps ``GET <url>?<sorted query>`` to a response
body and its provenance (``source``). Bodies were recorded on 2026-10-09: from
``docs/api/tillvaxtverket/samples/`` (columns of all six datasets, the
accommodation-type and year lists) and by this adapter's bounded live probes
(region, country, day-type and day lists, label files and data.europa.eu records,
trimmed); each entry says which, and which entries are reconstructed or
synthetic. ``fixtures/rows.json`` holds GuestNights_Capacity_Revenue_Year rows
for the fake server: 29 recorded rows (sources listed in its ``_provenance``)
and two labelled synthetic rows (a text marker in a measure, a missing value).
"""

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED: dict[str, Any] = {
    key: entry["body"]
    for key, entry in json.loads(
        (FIXTURES / "tillvaxtverket.json").read_text(encoding="utf-8")
    ).items()
}
QUERY = "https://oppnadata.tillvaxtverket.se/api/api/query/inkvartering/data"
DCAT = "https://data.europa.eu/api/hub/search/datasets"


def fixture_key(url: str, params=None) -> str:
    return f"GET {url}?{urlencode(sorted(params))}" if params else f"GET {url}"


def cbase(name: str) -> str:
    return f"{QUERY}/{name}.cbase"


def distinct_key(name: str, *columns: str) -> str:
    params = [("dimension", c) for c in columns]
    return fixture_key(cbase(name), [*params, ("format", "json"), ("limit", "100000")])


def probe_key(name: str) -> str:
    return fixture_key(cbase(name), [("format", "json"), ("limit", "0")])


def dcat_key(name: str) -> str:
    return fixture_key(
        f"{DCAT}/https-oppnadata-tillvaxtverket-se-dataset_{name.lower()}"
    )


def labels_key(name: str) -> str:
    return fixture_key(
        "https://oppnadata.tillvaxtverket.se/data/inkvartering/"
        f"{name}_variabeletiketter.csv"
    )


class RecordedClient:
    """Answer harvester requests from recorded responses, like the real client.

    Unrecorded requests raise ``LookupError``; keys in ``failing`` raise
    ``TimeoutError``. Subclasses map these to ``Unreachable``.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = copy.deepcopy(RECORDED)
        self.failing: set[str] = set()
        self.requests: list[str] = []
        self.request_count = 0

    def _answer(self, url: str, params) -> Any:
        key = fixture_key(url, params)
        self.requests.append(key)
        self.request_count += 1
        if key in self.failing:
            raise TimeoutError(f"timed out: {key}")
        if key not in self.responses:
            raise LookupError(f"not recorded: {key}")
        return copy.deepcopy(self.responses[key])

    async def get_json(self, url: str, params=None) -> Any:
        return self._answer(url, params)

    async def get_text(self, url: str, params=None, *, encoding=None) -> str:
        return self._answer(url, params)


@pytest.fixture
def client() -> RecordedClient:
    return RecordedClient()


# --- Retrieval: an in-process imitation of DivePort's query endpoint -----------

TABLE: dict[str, Any] = json.loads((FIXTURES / "rows.json").read_text("utf-8"))
NAME = "GuestNights_Capacity_Revenue_Year"
PATH = f"/api/api/query/inkvartering/data/{NAME}.cbase"


def _text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


class FakeDivePort:
    """Answer ``.cbase`` queries from ``rows.json`` as DivePort does.

    Supports ``filter:<COLUMN>=<value>`` (values of one column OR-ed, columns
    AND-ed), ``column=`` (restricts the returned columns, no grouping),
    ``limit`` (default 10,000) and ``offset``. Unknown files and columns answer
    HTTP 500 with DivePort's ``Dive failed`` messages (seen live 2026-10-09).
    """

    def __init__(self) -> None:
        self.rows = copy.deepcopy(TABLE["rows"])
        self.columns = [c["name"] for c in TABLE["columns"]]
        self.requests: list[list[tuple[str, str]]] = []
        self.urls: list[str] = []
        self.fail_status: int | None = None

    async def handle(self, request):
        from aiohttp import web

        name = request.match_info["name"]
        pairs = list(request.query.items())
        self.requests.append(pairs)
        self.urls.append(str(request.url))
        if self.fail_status is not None:
            return web.Response(status=self.fail_status, text="unavailable")
        if name != f"{NAME}.cbase":
            return web.json_response(
                {
                    "message": "Dive failed: remote dive request:5: File not "
                    f"found: /data/{name}"
                },
                status=500,
            )
        filters: dict[str, list[str]] = {}
        columns, limit, offset = [], 10_000, 0
        for key, value in pairs:
            if key.startswith("filter:"):
                filters.setdefault(key.split(":", 1)[1], []).append(value)
            elif key == "column":
                columns.append(value)
            elif key == "limit":
                limit = int(value)
            elif key == "offset":
                offset = int(value)
        for column in [*filters, *columns]:
            if column not in self.columns:
                return web.json_response(
                    {
                        "message": "Dive failed: remote dive request:7:18: "
                        f'Column "{column}" not found'
                    },
                    status=500,
                )
        rows = [
            row
            for row in self.rows
            if all(_text(row.get(c)) in values for c, values in filters.items())
        ]
        page = rows[offset : offset + limit]
        if columns:
            page = [{c: row.get(c) for c in columns} for row in page]
        return web.json_response(
            {
                "columns": [
                    c for c in TABLE["columns"] if not columns or c["name"] in columns
                ],
                "dimensionCount": 0,
                "rowCount": len(rows),
                "rows": page,
            }
        )


@pytest.fixture
def fake() -> FakeDivePort:
    return FakeDivePort()


def metadata_for(document_extension: dict | None = None) -> dict:
    """``REQUIRED_METADATA`` of the Revenue_Year dataset."""
    native = document_extension or {
        "area": "inkvartering",
        "name": NAME,
        "region_dimension": "Region",
        "measure_dimension": "ContentsCode",
        "dimension_columns": {"Anlaggningstyp": "ANLAGGNINGSTYP_NAMN", "Tid": "AR"},
        "columns": TABLE["columns"],
    }
    return {
        "/id": ["Region", "Anlaggningstyp", "Tid", "ContentsCode"],
        "/extension/tillvaxtverket": native,
    }


async def retrieve_from(fake: FakeDivePort, selection, **kwargs):
    """Call ``retrieve`` against ``fake``; return the fragment."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from retrieval.adapters.tillvaxtverket import retrieve

    app = web.Application()
    app.router.add_get("/api/api/query/inkvartering/data/{name}", fake.handle)
    async with TestServer(app) as server:
        async with aiohttp.ClientSession() as session:
            name = kwargs.pop("name", NAME)
            arguments = {
                "provider_code": "tillvaxtverket",
                "dataset_code": f"inkvartering/{name}",
                "language": "sv",
                "selection": selection,
                "data_url": str(
                    server.make_url(
                        f"/api/api/query/inkvartering/data/{name}.cbase"
                    ).with_query({"format": "json"})
                ),
                "metadata_url": None,
                "metadata": metadata_for(),
                **kwargs,
            }
            fragment = await retrieve(session, **arguments)
            assert not session.closed
            return fragment


def check_fragment(fragment: dict[str, Any], selection) -> None:
    """Validate a fragment: result schema plus the contract's consumer checks."""
    from harvesting.validation import validate_retrieval

    validate_retrieval(fragment, selection=selection)
    ids = fragment["id"]
    assert ids == list(fragment["dimension"])
    assert set(ids) == set(selection)
    total = 1
    for name, size in zip(ids, fragment["size"], strict=True):
        codes = fragment["dimension"][name]["category"]["index"]
        assert size == len(codes)
        assert codes == list(selection[name])
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

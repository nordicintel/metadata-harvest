"""Shared fixtures: recorded Försäkringskassan responses and a stand-in table API.

Provenance:

- ``fixtures/recorded.json`` maps ``GET <url>`` to a response body. Recorded
  2026-10-09 (docs/api/forsakringskassan/samples): the two ``meta.json`` files
  (``ase-avslutsanledning``, ``ase-beslut-lan``) and the texts excerpt
  (``textnycklar-meka.excerpt.json``). The ``/reports`` catalogue is trimmed from
  ``samples/reports.json`` to the ``ase`` group's two sampled reports plus a
  synthetic group ``syn``. **Synthetic**: ``syn-alias`` (a copy of
  ``ase-beslut-lan`` with the key variants ``lan``/``kon``, a birth-year ``ar`` and
  a ``belopp`` measure), ``syn-kommun`` (a copy whose ``table.order`` names the
  undeclared column ``kommun_kod``, as 27 real reports do), their text entries
  and their one-row probe answers.
- ``fixtures/tables.json`` holds table rows recorded 2026-10-09:
  ``ase-avslutsanledning.ASEAvslutsanledning.json`` (complete, 405 rows, 57 with
  ``rojd: false`` and ``value: null``) and
  ``ase-beslut-lan.ASEBeslutLan.trimmed.json`` (61 unique rows, 3 with
  ``rojd: true``).
"""

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "recorded.json").read_text(encoding="utf-8")
)
TABLES: dict[str, list] = json.loads(
    (FIXTURES / "tables.json").read_text(encoding="utf-8")
)


def fixture_key(url: str) -> str:
    """``GET <url>`` with sorted query parameters."""
    parts = urlsplit(url)
    query = sorted(parse_qsl(parts.query, keep_blank_values=True))
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"GET {base}?{urlencode(query)}" if query else f"GET {base}"


class RecordedClient:
    """Answer harvester requests from recorded responses, like the real client.

    Unrecorded requests and keys in ``failing`` raise ``Unreachable``.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {
            fixture_key(key[4:]): value
            for key, value in copy.deepcopy(RECORDED).items()
        }
        self.failing: set[str] = set()
        self.requests: list[str] = []
        self.request_count = 0

    def respond(self, url: str, body: Any) -> None:
        self.responses[fixture_key(url)] = body

    def body(self, url: str) -> Any:
        return self.responses[fixture_key(url)]

    def fail(self, url: str) -> None:
        self.failing.add(fixture_key(url))

    async def get_json(self, url: str) -> Any:
        from metadata_harvest.adapters.forsakringskassan.client import Unreachable

        key = fixture_key(url)
        self.requests.append(key)
        self.request_count += 1
        if key in self.failing:
            raise Unreachable(f"timed out: {key}")
        if key not in self.responses:
            raise Unreachable(f"not recorded: {key}")
        return copy.deepcopy(self.responses[key])


@pytest.fixture
def client() -> RecordedClient:
    return RecordedClient()


# --- Retrieval: an in-process imitation of the table endpoint -----------------------


class FakeTables:
    """Answer ``/{report}/{Table}.json`` and ``/{report}/meta.json`` like the API.

    Reproduces behaviour observed on 2026-10-09: rows filtered by ``dim=a,b``
    (only the first of repeated parameters counts), ``[]`` for unknown codes and for
    unknown tables of a known report, HTTP 404 for unknown reports.
    """

    def __init__(self) -> None:
        self.tables = copy.deepcopy(TABLES)
        self.metas = {
            key.rsplit("/", 2)[-2]: value
            for key, value in RECORDED.items()
            if key.endswith("/meta.json")
        }
        self.requests: list[str] = []
        self.fail_status: int | None = None

    async def handle(self, request):
        from aiohttp import web

        self.requests.append(str(request.rel_url))
        if self.fail_status is not None:
            return web.Response(status=self.fail_status, text="unavailable")
        report, name = request.match_info["report"], request.match_info["name"]
        if report not in self.metas:
            return web.Response(status=404)
        if name == "meta":
            return web.json_response(self.metas[report])
        rows = self.tables.get(f"{report}/{name}", [])
        filters = {}
        for key in request.query:
            if key not in filters:
                filters[key] = request.query.getone(key).split(",")
        selected = [
            row
            for row in rows
            if all(row["dimensions"].get(k) in codes for k, codes in filters.items())
        ]
        return web.json_response(selected)


@pytest.fixture
def fake() -> FakeTables:
    return FakeTables()


async def retrieve_from(fake: FakeTables, selection, **kwargs):
    """Call ``retrieve`` against ``fake``; return the fragment."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from metadata_harvest.adapters.forsakringskassan.retrieval import retrieve

    app = web.Application()
    app.router.add_get("/v1/{report}/{name}.json", fake.handle)
    dataset = kwargs.pop("dataset_code", "ase-beslut-lan/ASEBeslutLan")
    report, table = dataset.split("/")
    async with TestServer(app) as server:
        async with aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "forsakringskassan",
                "dataset_code": dataset,
                "language": "sv",
                "selection": selection,
                "data_url": str(server.make_url(f"/v1/{report}/{table}.json")),
                "metadata_url": str(server.make_url(f"/v1/{report}/meta.json")),
                "metadata": {
                    "/id": list(selection),
                    "/extension/forsakringskassan": {
                        "report": report,
                        "table": table,
                        "measure_dimension": "matt",
                    },
                },
                **kwargs,
            }
            fragment = await retrieve(session, **arguments)
            assert not session.closed
            # The caller's session stays usable after retrieval.
            async with session.get(server.make_url(f"/v1/{report}/meta.json")) as r:
                assert r.status == 200
            return fragment


def check_fragment(fragment: dict[str, Any], selection) -> None:
    """Validate a fragment: result schema plus the contract's consumer checks."""
    from metadata_harvest.validation import validate_retrieval

    validate_retrieval(fragment, selection=selection)
    ids = fragment["id"]
    assert ids == list(fragment["dimension"]) and set(ids) == set(selection)
    total = 1
    for name, size in zip(ids, fragment["size"], strict=True):
        index = fragment["dimension"][name]["category"]["index"]
        assert size == len(index) and sorted(index) == sorted(selection[name])
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

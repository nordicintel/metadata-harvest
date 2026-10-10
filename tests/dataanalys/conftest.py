"""Shared fixtures: recorded analysverktyg answers served by stand-ins.

Provenance:

- ``fixtures/recorded.json``: real answers recorded on 2026-10-09 from
  ``https://dataanalys.socialstyrelsen.se`` by a bounded live harvest (areas GONV,
  NRAA, LRHST; indicators G082, G013, G033, G200, NRAA_1, NRAA_2, LRHST09), partly
  answered from the research recordings of the same day (research sample folder
  ``docs/api/socialstyrelsen-superset/samples/``, local and git-ignored). Keyed by
  ``GET url?sorted-query`` or ``POST url <canonical JSON body>``. Trimmed: the chart
  list to 33 of 492 rows (one MART chart kept), the dashboard list to 10 of 96,
  catalogues to the fixture indicators, large dimension answers to Riket, Stockholm
  and Gotland over four periods; SQL text and cache fields dropped. Each entry
  states its own trimming in ``provenance``. The HTTP 400 answers (LRHST catalogue
  without ``MTypeName``; NRAA and LRHST values tables with native column names) are
  verbatim.
- ``fixtures/values.json``: every Riket and Region row of G082 in
  ``semantic_VAI_GONV_Trender``, recorded 2026-10-09, for the retrieval imitation.
- ``fixtures/synthetic.json``: labelled synthetic additions (a malformed catalogue
  row, a duplicate ``MBusinessKey``, a broken catalogue link, an unpublished
  dashboard), applied only by the tests that name them.
"""

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from harvesting.adapters.dataanalys.client import RequestRejected, Unreachable

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "recorded.json").read_text(encoding="utf-8")
)
SYNTHETIC: dict[str, Any] = json.loads(
    (FIXTURES / "synthetic.json").read_text(encoding="utf-8")
)
VALUES: dict[str, Any] = json.loads(
    (FIXTURES / "values.json").read_text(encoding="utf-8")
)
PROVIDER = {
    "code": "socialstyrelsen_analys",
    "languages": ["sv"],
    "max_age_days": 60,
    "retry_cooldown_seconds": 0,
}
AREA_IDS = {"GONV": (160, 159), "NRAA": (165, 164), "LRHST": (158, 163)}
"""Catalogue and values datasource ids."""


def get_key(url: str, params: dict[str, str] | None) -> str:
    return f"GET {url}?{urlencode(sorted((params or {}).items()))}"


def post_key(url: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return f"POST {url} {canonical}"


def _query(key: str) -> dict[str, Any] | None:
    if not key.startswith("POST "):
        return None
    return json.loads(key.split(" ", 2)[2])


class RecordedClient:
    """Answer harvester requests from recorded answers, like ``DataanalysClient``.

    Unrecorded requests raise ``LookupError``; keys in ``failing`` raise
    :class:`Unreachable`; recorded 4xx answers raise :class:`RequestRejected`.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = copy.deepcopy(RECORDED)
        self.failing: set[str] = set()
        self.requests: list[str] = []
        self.request_count = 0

    def _answer(self, key: str, method: str, url: str) -> Any:
        self.requests.append(key)
        self.request_count += 1
        if key in self.failing:
            raise Unreachable(f"timed out: {key[:120]}")
        if key not in self.responses:
            raise LookupError(f"not recorded: {key[:300]}")
        entry = self.responses[key]
        if entry["status"] != 200:
            raise RequestRejected(method, url, entry["status"], entry["text"])
        return copy.deepcopy(entry["body"])

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        return self._answer(get_key(url, params), "GET", url)

    async def post_json(self, url: str, body: dict[str, Any]) -> Any:
        return self._answer(post_key(url, body), "POST", url)

    async def close(self) -> None:
        pass

    # --- lookups ---------------------------------------------------------------

    def listing(self, kind: str) -> dict[str, Any]:
        """The (mutable) chart or dashboard list body."""
        return next(
            entry["body"]
            for key, entry in self.responses.items()
            if key.startswith(
                f"GET https://dataanalys.socialstyrelsen.se/api/v1/{kind}/"
            )
        )

    def keys(self, *, datasource: int, kind: str, code: str | None = None) -> list[str]:
        """Recorded POST keys: ``catalogue``, ``probe`` or ``cube`` queries."""
        found = []
        for key in self.responses:
            body = _query(key)
            if body is None or body["datasource"]["id"] != datasource:
                continue
            query = body["queries"][0]
            filters = {f["col"]: f["val"] for f in query["filters"]}
            if (
                kind == "catalogue"
                and "GroupTypeName" in filters
                and query["row_limit"] == 1000
            ):
                found.append(key)
            elif kind == "probe" and query["row_limit"] == 1:
                found.append(key)
            elif kind == "cube" and query["row_limit"] == 20000:
                if code is None or code in (
                    filters.get("Matt_id") or filters.get("MBusinessKey") or []
                ):
                    found.append(key)
        return found

    def catalogue_rows(self, area: str) -> list[dict[str, Any]]:
        """The (mutable) rows of the area's successful catalogue answer."""
        for key in self.keys(datasource=AREA_IDS[area][0], kind="catalogue"):
            entry = self.responses[key]
            if entry["status"] == 200:
                return entry["body"]["result"][0]["data"]
        raise LookupError(area)

    def cube_key(self, area: str, code: str) -> str:
        [key] = self.keys(datasource=AREA_IDS[area][1], kind="cube", code=code)
        return key

    def catalogue_key(self, area: str) -> str:
        return next(
            key
            for key in self.keys(datasource=AREA_IDS[area][0], kind="catalogue")
            if self.responses[key]["status"] == 200
        )


def apply_synthetic(client: RecordedClient, *names: str) -> RecordedClient:
    """Apply the named synthetic additions from ``fixtures/synthetic.json``."""
    for name in names:
        item = copy.deepcopy(SYNTHETIC[name])
        if name == "malformed_row":
            client.catalogue_rows(item["area"]).insert(item["position"], item["row"])
        elif name == "duplicate_row":
            rows = client.catalogue_rows(item["area"])
            original = next(r for r in rows if r["MBusinessKey"] == item["copy_of"])
            rows.append({**original, **item["changes"]})
        elif name == "broken_href":
            rows = client.catalogue_rows(item["area"])
            next(r for r in rows if r["MBusinessKey"] == item["code"])[
                "HrefIndicatorDashboard"
            ] = item["href"]
        elif name == "unpublished_dashboard":
            listing = client.listing("dashboard")
            listing["result"].append(item["dashboard"])
            listing["count"] += 1
            rows = client.catalogue_rows(item["area"])
            next(r for r in rows if r["MBusinessKey"] == item["code"])[
                "HrefIndicatorDashboard"
            ] = item["href"]
        else:
            raise KeyError(name)
        for entry in client.responses.values():  # keep rowcount consistent
            body = entry.get("body")
            if isinstance(body, dict) and "result" in body and "count" not in body:
                body["result"][0]["rowcount"] = len(body["result"][0]["data"])
    return client


@pytest.fixture
def client() -> RecordedClient:
    return RecordedClient()


# --- Retrieval: an in-process imitation of Superset's POST /api/v1/chart/data -----


class FakeSuperset:
    """Answer ``/chart/data`` queries from G082's recorded rows, as Superset does.

    Reproduces behaviour observed on 2026-10-09: HTTP 404 ``{"message": "Not
    found"}`` for an unknown datasource; HTTP 400 ``Columns missing in dataset``
    for unknown columns; ``metrics: []`` returns distinct rows of the requested
    columns (``GROUP BY``), at most ``row_limit`` of them.
    """

    def __init__(self) -> None:
        self.datasource_id = VALUES["datasource_id"]
        self.rows = copy.deepcopy(VALUES["rows"])
        self.bodies: list[dict[str, Any]] = []
        self.fail_status: int | None = None
        self.query_status: str | None = None

    @property
    def queries(self) -> list[dict[str, Any]]:
        return [body["queries"][0] for body in self.bodies]

    async def handle(self, request):
        from aiohttp import web

        body = await request.json()
        self.bodies.append(body)
        if self.fail_status is not None:
            return web.Response(status=self.fail_status, text="unavailable")
        if body["datasource"]["id"] != self.datasource_id:
            return web.json_response({"message": "Not found"}, status=404)
        query = body["queries"][0]
        known = set(self.rows[0])
        if missing := [c for c in query["columns"] if c not in known]:
            return web.json_response(
                {"message": f"Error: Columns missing in dataset: {missing}"},
                status=400,
            )
        if self.query_status == "failed":
            return web.json_response(
                {
                    "result": [
                        {"status": "failed", "error": "Query timed out", "data": []}
                    ]
                }
            )
        rows, seen = [], set()
        for row in self.rows:
            if any(row.get(f["col"]) not in f["val"] for f in query["filters"]):
                continue
            projected = {column: row[column] for column in query["columns"]}
            marker = json.dumps(projected, sort_keys=True)
            if marker not in seen:
                seen.add(marker)
                rows.append(projected)
        rows = rows[: query["row_limit"]]
        return web.json_response(
            {
                "result": [
                    {
                        "status": "success",
                        "error": None,
                        "rowcount": len(rows),
                        "colnames": query["columns"],
                        "data": rows,
                    }
                ]
            }
        )


@pytest.fixture
def fake_superset() -> FakeSuperset:
    return FakeSuperset()


def harvested_g082() -> dict[str, Any]:
    """The harvested G082 document, built from the recorded answers."""
    import asyncio

    from harvesting.adapters.dataanalys.workflow import fetch_metadata

    [document] = asyncio.run(
        fetch_metadata(
            {"area": "GONV", "indicators": ["G082"]}, client=RecordedClient()
        )
    )
    return document


@pytest.fixture(scope="session")
def g082() -> dict[str, Any]:
    return harvested_g082()


def metadata_of(document: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``REQUIRED_METADATA`` as the service does."""
    from harvesting.adapters.dataanalys.retrieval import REQUIRED_METADATA

    resolved = {}
    for pointer in REQUIRED_METADATA:
        value: Any = document
        for part in pointer.strip("/").split("/"):
            value = value[part]
        resolved[pointer] = copy.deepcopy(value)
    return resolved


async def retrieve_from(fake: FakeSuperset, document, selection, **overrides):
    """Call ``retrieve`` against ``fake``; the caller's session must stay open."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from harvesting.adapters.dataanalys.retrieval import retrieve

    app = web.Application()
    app.router.add_post("/api/v1/chart/data", fake.handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        shared = document["extension"]["nordicintel"]
        arguments = {
            "provider_code": shared["provider_code"],
            "dataset_code": shared["dataset_code"],
            "language": shared["language"],
            "selection": selection,
            "data_url": str(server.make_url("/api/v1/chart/data")),
            "metadata_url": shared.get("metadata_url"),
            "metadata": metadata_of(document),
            **overrides,
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
        index = fragment["dimension"][name]["category"]["index"]
        assert size == len(index)
        assert sorted(index) == sorted(selection[name])
        total *= size
    for field in ("value", "status"):
        content = fragment.get(field)
        if isinstance(content, list):
            assert len(content) == total
        elif isinstance(content, dict):
            assert all(int(key) < total for key in content)

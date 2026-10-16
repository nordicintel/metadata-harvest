"""EntryScape RowStore fixtures: canned RowStore pages for harvest and retrieval."""

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from apps.harvest.metadata_adapters.entryscape.client import Client
from tests.support.binding import binding

PAGE = 500


def filter_rows(rows: list[dict], params: dict) -> list[dict]:
    """Apply RowStore column filters: ``^`` regex (search), otherwise exact."""
    matched = rows
    for column, wanted in params.items():
        if column.startswith("_"):
            continue
        if wanted.startswith("^"):
            pattern = re.compile(wanted)
            matched = [r for r in matched if pattern.search(str(r.get(column, "")))]
        else:
            matched = [r for r in matched if str(r.get(column, "")) == wanted]
    return matched


def page(rows: list[dict], params: dict) -> dict:
    limit = int(params.get("_limit", 100))
    limit = limit if 1 <= limit <= PAGE else 100
    offset = int(params.get("_offset", 0))
    matched = filter_rows(rows, params)
    return {
        "resultCount": len(matched),
        "offset": offset,
        "limit": limit,
        "results": matched[offset : offset + limit],
    }


class FakeRowStore:
    """``/rowstore/dataset/{uuid}`` served from in-memory rows, per table uuid."""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        if len(str(request.url)) > 9000:
            return httpx.Response(414, text="Request-URI Too Long")
        code = url.path.rsplit("/", 1)[1]
        params = dict(parse_qsl(url.query, keep_blank_values=True))
        self.calls.append((code, params))
        if code not in self.tables:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, json=page(self.tables[code], params))


@pytest.fixture
def transport_factory():
    def make(tables: dict[str, list[dict]]):
        fake = FakeRowStore(tables)
        return httpx.MockTransport(fake), fake

    return make


NOW = datetime(2026, 11, 1, 6, tzinfo=UTC)
DCAT = "http://www.w3.org/ns/dcat#"
DCTERMS = "http://purl.org/dc/terms/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"
PROVIDERS = [
    binding(
        "entryscape",
        "alpha",
        "https://alpha.example",
        label="Alpha kommun",
        languages=["sv"],
    ),
    binding(
        "entryscape",
        "beta",
        "https://beta.example",
        label="Beta kommun",
        languages=["sv"],
    ),
]


def rows(n, prefix="A"):
    return [
        {
            "ar": str(2020 + i % 3),
            "omrade": f"{prefix}{i % 4}",
            "antal": str(i),
            "kommun_id": "1",
        }
        for i in range(n)
    ]


class FakeEntryScape:
    """EntryStore search plus RowStore tables for several hosts in one transport."""

    def __init__(self):
        self.tables: dict[
            str, dict
        ] = {}  # host -> uuid -> {"rows", "title", "modified"}
        self.down: set[str] = set()  # hosts whose search fails
        self.throttle: set[str] = set()  # hosts answering 429
        self.calls: list[str] = []

    def add(self, host, uuid, title, rows, modified="2026-01-01T00:00:00Z"):
        self.tables.setdefault(host, {})[uuid] = {
            "rows": rows,
            "title": title,
            "modified": modified,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        host = url.hostname
        self.calls.append(f"{host}{url.path}")
        if host in self.throttle:
            return httpx.Response(429, headers={"Retry-After": "1"})
        if url.path.endswith("/store/search"):
            if host in self.down:
                return httpx.Response(500)
            query = dict(parse_qsl(url.query)).get("query", "")
            if query.startswith("resource:("):
                return httpx.Response(
                    200, json={"resource": {"children": self._datasets(host)}}
                )
            if "rowstore" not in query:
                return httpx.Response(200, json={"resource": {"children": []}})
            return httpx.Response(
                200, json={"resource": {"children": self._distributions(host)}}
            )
        if "/rowstore/dataset/" in url.path:
            uuid = url.path.rsplit("/", 1)[1]
            table = self.tables.get(host, {}).get(uuid)
            if table is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=page(
                    table["rows"], dict(parse_qsl(url.query, keep_blank_values=True))
                ),
            )
        return httpx.Response(404)

    def _distributions(self, host):
        children = []
        for uuid, table in self.tables.get(host, {}).items():
            dist = f"https://{host}/store/1/resource/d-{uuid}"
            dataset = f"https://{host}/store/1/resource/s-{uuid}"
            metadata = {
                RDF_TYPE: [{"value": DCAT + "Distribution"}],
                DCAT + "accessURL": [
                    {"value": f"https://{host}/rowstore/dataset/{uuid}"}
                ],
                DCTERMS + "title": [{"value": table["title"] + " - API", "lang": "sv"}],
            }
            if table["modified"]:
                metadata[DCTERMS + "modified"] = [{"value": table["modified"]}]
            children.append(
                {
                    "contextId": "1",
                    "entryId": "d-" + uuid,
                    "metadata": {dist: metadata},
                    "relations": {dataset: {DCAT + "distribution": [{"value": dist}]}},
                }
            )
        return children

    def _datasets(self, host):
        return [
            {
                "contextId": "1",
                "entryId": "s-" + uuid,
                "metadata": {
                    f"https://{host}/store/1/resource/s-{uuid}": {
                        RDF_TYPE: [{"value": DCAT + "Dataset"}],
                        DCTERMS + "title": [{"value": table["title"], "lang": "sv"}],
                    }
                },
            }
            for uuid, table in self.tables.get(host, {}).items()
        ]


@pytest.fixture
def source():
    fake = FakeEntryScape()
    fake.add("alpha.example", UUID_A, "Befolkning 2023", rows(40))
    fake.add("alpha.example", UUID_B, "Befolkning 2024", rows(40, "B"), modified=None)
    fake.add("beta.example", UUID_C, "Lekplatser", rows(10))
    return fake


def execute(fake, data, output, **selection):
    """Run the adapter against ``fake`` and collect the output into ``data``."""
    from apps.harvest.metadata_adapters.entryscape.harvest import ADAPTER
    from harvest import lifecycle

    transport = httpx.MockTransport(fake)

    def factory(base_url, pacer):
        return Client(base_url, pacer=pacer, transport=transport)

    result = asyncio.run(
        lifecycle.run(
            ADAPTER,
            PROVIDERS,
            data,
            output,
            selection={"preflight": False, "requests_per_second": 1e6, **selection},
            client=factory,
            clock=NOW,
        )
    )
    lifecycle.collect(ADAPTER, data, output)
    return result

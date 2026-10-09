"""EntryScape RowStore fixtures: canned RowStore pages for harvest and retrieval."""

import re
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

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

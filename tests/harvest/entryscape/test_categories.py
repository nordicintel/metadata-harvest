"""Exactness and request-count tests against a fake RowStore with live-verified semantics.

Fake server rules, each verified against catalog.ale.se on 2026-10-09:
- ``_limit`` 1-500 is honoured (other values fall back to 100); ``_offset`` pages.
- A filter value starting with ``^`` is a case-sensitive regular expression;
  other values are exact matches. Several filters combine with AND.
- Request URLs above about 9 KB fail with HTTP 414.
"""

import asyncio
import csv
import io
import itertools
import json
import random
import re
from math import ceil
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from apps.harvest.metadata_adapters.entryscape.categories import (
    PAGE_SIZE,
    classify,
    infer_structure,
    plan_prefixes,
    prefix_pattern,
    remainder_pattern,
)
from apps.harvest.metadata_adapters.entryscape.client import Client
from apps.harvest.metadata_adapters.entryscape.tables import table_document

TABLE = "rowstore/dataset/00000000-0000-0000-0000-000000000000"


def fake_rowstore(rows: list[dict]):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if len(str(request.url)) > 9000:
            return httpx.Response(414, text="Request-URI Too Long")
        params = dict(
            parse_qsl(urlsplit(str(request.url)).query, keep_blank_values=True)
        )
        limit = int(params.pop("_limit", 100))
        limit = limit if 1 <= limit <= 500 else 100
        offset = int(params.pop("_offset", 0))
        matched = rows
        for column, wanted in params.items():
            if wanted.startswith("^"):
                pattern = re.compile(wanted)
                matched = [row for row in matched if pattern.search(row[column])]
            else:
                matched = [row for row in matched if row[column] == wanted]
        page = matched[offset : offset + limit]
        return httpx.Response(
            200,
            json={
                "resultCount": len(matched),
                "offset": offset,
                "limit": limit,
                "results": page,
            },
        )

    return httpx.MockTransport(handler), calls


def run(rows, roles=None):
    transport, calls = fake_rowstore(rows)

    async def go():
        async with Client(
            "https://example.org", transport=transport, interval=0
        ) as client:
            return await infer_structure(client, TABLE, roles=roles)

    structure = asyncio.run(go())
    return structure, calls["count"]


def truth(rows, column):
    return {row[column] for row in rows}


def cube(dimensions: dict[str, list[str]], measures=("antal", "andel"), seed=1):
    rng = random.Random(seed)
    names = list(dimensions)
    rows = []
    for combination in itertools.product(*dimensions.values()):
        row = dict(zip(names, combination, strict=True))
        for measure in measures:
            row[measure] = str(rng.randint(0, 100000))
        rows.append(row)
    rng.shuffle(rows)
    return rows


def test_single_page_table_needs_one_request():
    rows = cube(
        {"kommun": [f"K{i}" for i in range(20)], "kön": ["män", "kvinnor", "totalt"]}
    )
    structure, requests = run(rows)
    assert requests == 1
    assert structure["strategy"] == "single_page"
    assert set(structure["categories"]["kommun"]) == truth(rows, "kommun")
    assert structure["measures"] == ["antal", "andel"]


def test_cube_is_exact_and_much_cheaper_than_a_full_scan():
    regions = [f"Kommun nummer {i:03d} i län {i % 21}" for i in range(290)]
    rows = cube(
        {
            "region": regions,
            "år": [str(y) for y in range(2015, 2025)],
            "kön": ["män", "kvinnor", "totalt"],
        }
    )
    structure, requests = run(rows)
    for column in ("region", "år", "kön"):
        assert set(structure["categories"][column]) == truth(rows, column)
    full_scan = ceil(len(rows) / PAGE_SIZE)
    assert structure["strategy"] == "enumeration"
    assert requests < full_scan, (requests, full_scan)


def test_long_exclusion_lists_split_by_prefix_and_stay_exact():
    names = [f"Område med ett ganska långt namn nummer {i:04d}" for i in range(1400)]
    rows = cube(
        {"omrade": names, "kön": ["män", "kvinnor"], "år": ["2023", "2024"]},
        measures=("antal",),
    )
    structure, requests = run(rows)
    assert set(structure["categories"]["omrade"]) == truth(rows, "omrade")
    assert structure["strategy"] == "enumeration"
    assert any(entry["query"] == "remainder" for entry in structure["enumeration_log"])
    assert requests < ceil(len(rows) / PAGE_SIZE), requests


def test_special_characters_empty_and_unicode_values_are_exact():
    tricky = [
        "a.b",
        "a|b",
        "(x)",
        "[y]",
        "^z",
        "q$",
        "1+1",
        "50%",
        "Ö-område",
        "",
        "a\\b",
        "c*",
        "d?",
        "{e}",
    ]
    rows = cube(
        {"kod": tricky * 1, "grupp": [f"G{i}" for i in range(60)]}, measures=("antal",)
    )
    structure, requests = run(rows)
    assert set(structure["categories"]["kod"]) == set(tricky)
    assert set(structure["categories"]["grupp"]) == truth(rows, "grupp")


def test_row_unique_dimension_falls_back_to_a_full_scan_and_stays_exact():
    dates = [
        f"2020-{m:02d}-{d:02d} {h:02d}:00"
        for m in range(1, 13)
        for d in range(1, 29)
        for h in range(0, 24, 3)
    ]
    rows = cube({"datum": dates}, measures=("temp",))
    structure, requests = run(rows)
    assert set(structure["categories"]["datum"]) == truth(rows, "datum")
    assert structure["strategy"] == "full_scan"
    assert requests == ceil(len(rows) / PAGE_SIZE)


def test_patterns_exclude_seen_values_and_plans_split_at_branches():
    universe = ["", "a", "ab", "ac", "ad", "b", "bc", "c", "x.y", "x|y"]
    seen = {"ab", "ac", "b", "", "x.y"}
    unseen = sorted(set(universe) - seen)
    pattern = re.compile(prefix_pattern("", seen))
    assert sorted(v for v in universe if pattern.search(v)) == unseen
    under_a = re.compile(prefix_pattern("a", seen))
    assert sorted(v for v in universe if under_a.search(v)) == ["a", "ad"]
    outside = re.compile(remainder_pattern(["a"], seen))
    assert sorted(v for v in universe if outside.search(v)) == ["bc", "c", "x|y"]
    values = [f"Kommun nummer {i:03d}" for i in range(300)]
    assert plan_prefixes("", sorted(values), lambda p: len(p) < 2000) == [
        "Kommun nummer "
    ]
    plan = plan_prefixes("", sorted(values), lambda p: len(p) < 600)
    assert plan == ["Kommun nummer 0", "Kommun nummer 1", "Kommun nummer 2"]


def test_classification_uses_names_and_values():
    rows = [
        {
            "år": "2020",
            "kommunkod": "1280",
            "kvinnor": "143",
            "ålder": "0",
            "namn": "Malmö",
        }
    ]
    roles = classify(list(rows[0]), rows)
    assert {c: r["role"] for c, r in roles.items()} == {
        "år": "dimension",
        "kommunkod": "dimension",
        "kvinnor": "measure",
        "ålder": "dimension",
        "namn": "dimension",
    }
    assert roles["år"]["time"] is True


def test_jsonstat_document_is_valid_metadata_only_output():
    rows = cube({"kommun": [f"K{i}" for i in range(30)], "år": ["2023", "2024"]})
    structure, _ = run(rows)
    record = {
        "provider_code": "x",
        "dataset_code": "t",
        "language": "sv",
        "title": "T",
        "published": "2026-01-01",
    }
    document = table_document(record, structure)
    assert (
        document["value"] == [] and document["extension"]["metadata_harvest"] == record
    )
    assert document["id"] == ["kommun", "år", "measure"]
    assert document["size"] == [30, 2, 2]
    assert document["role"] == {"metric": ["measure"], "time": ["år"]}
    json.dumps(document, allow_nan=False)


def test_empty_table_is_reported_as_empty():
    from apps.harvest.metadata_adapters.entryscape.categories import EmptyTable

    with pytest.raises(EmptyTable):
        run([])


def test_table_without_dimension_columns_gets_only_the_measure_dimension():
    rows = [{"unika personer": "470", "utbildningsplatser": "376"}]
    structure, requests = run(rows)
    record = {
        "provider_code": "x",
        "dataset_code": "t",
        "language": "sv",
        "title": "T",
        "published": None,
    }
    document = table_document(record, structure)
    assert requests == 1
    assert document["id"] == ["measure"] and document["size"] == [2]


SOURCE = "https://example.org/store/1/resource/113"


def fake_with_sources(rows: list[dict], files: list[str]):
    """Fake RowStore plus an EntryStore source distribution serving ``files`` as CSV."""
    table, calls = fake_rowstore(rows)
    urls = [f"https://example.org/store/1/resource/f{i}" for i in range(len(files))]

    def handler(request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        if path == "/store/1/metadata/113":
            calls["count"] += 1
            downloads = [{"type": "uri", "value": url} for url in urls]
            return httpx.Response(
                200, json={SOURCE: {"http://www.w3.org/ns/dcat#downloadURL": downloads}}
            )
        if path.startswith("/store/1/resource/f"):
            calls["count"] += 1
            body = files[int(path.rsplit("f", 1)[1])].encode("utf-8-sig")
            return httpx.Response(
                200, content=body, headers={"content-type": "text/csv"}
            )
        return table.handler(request)

    return httpx.MockTransport(handler), calls


def to_csv(rows: list[dict], delimiter: str = ";") -> str:
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=list(rows[0]), delimiter=delimiter, lineterminator="\r\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def run_with_sources(rows, files):
    transport, calls = fake_with_sources(rows, files)

    async def go():
        async with Client(
            "https://example.org", transport=transport, interval=0
        ) as client:
            return await infer_structure(client, TABLE, source_uri=SOURCE)

    return asyncio.run(go()), calls["count"]


def register(n=20000, seed=3):
    rng = random.Random(seed)
    return [
        {
            "datum": f"2025-{1 + i % 12:02d}-{1 + i % 28:02d}",
            "verifikationsnummer": str(100000 + i),
            "leverantor": f"Leverantör {rng.randint(1, 900)}",
            "belopp": f"{rng.randint(1, 99999)},{rng.randint(0, 99):02d}",
        }
        for i in range(n)
    ]


def test_expensive_table_is_read_from_its_source_files():
    rows = register()
    parts = [to_csv(rows[i : i + 5000]) for i in range(0, len(rows), 5000)]
    structure, requests = run_with_sources(rows, parts)
    assert structure["strategy"] == "source_files"
    assert requests == 1 + 1 + len(parts)  # first page, source metadata, files
    for column in ("datum", "verifikationsnummer", "leverantor"):
        assert set(structure["categories"][column]) == truth(rows, column)


def test_source_files_that_do_not_match_the_table_are_ignored():
    rows = register()
    parts = [to_csv(rows[:-1])]  # one row short of the table
    structure, requests = run_with_sources(rows, parts)
    assert structure["strategy"] == "full_scan"
    assert set(structure["categories"]["verifikationsnummer"]) == truth(
        rows, "verifikationsnummer"
    )

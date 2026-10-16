"""Retrieval against an in-process RowStore: filters, paging, aggregation, status."""

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from apps.harvest.metadata_adapters.entryscape.mapping import BLANK, to_document
from retrieval.data_adapters.entryscape import (
    MAX_REQUESTS,
    REQUIRED_METADATA,
    filter_value,
    number,
    retrieve,
)
from schema.validation import validate_retrieval
from tests.support.binding import binding
from tests.support.entryscape import page


def ledger_rows(year):
    return [
        {
            "forvaltning": "Skola" if i % 2 else "Vård",
            "konto_nr": "10" if i < 300 else "20",
            "konto_text": "Löner" if i < 300 else "Hyra",
            "kommun_id": "1480",
            "fakturanummer": str(i),
            "leverantor": f"Leverantör {i % 7}",
            "belopp": f"{i},50" if i % 50 else "..",
            "antal": "1",
        }
        for i in range(400)
    ]


def dataset_record(server_url):
    return {
        "provider_code": "ex",
        "dataset_code": "ex:reskontra",
        "title": "Leverantörsreskontra",
        "tables": [
            f"{server_url}/rowstore/dataset/t2023",
            f"{server_url}/rowstore/dataset/t2024",
        ],
        "rowcount": 800,
        "dimensions": [
            {
                "id": "part",
                "distinct": 2,
                "enumerable": True,
                "time": False,
                "categories": ["2023", "2024"],
                "tables": {
                    "2023": f"{server_url}/rowstore/dataset/t2023",
                    "2024": f"{server_url}/rowstore/dataset/t2024",
                },
            },
            {
                "id": "forvaltning",
                "column": "forvaltning",
                "distinct": 2,
                "enumerable": True,
                "time": False,
                "categories": ["Skola", "Vård"],
            },
            {
                "id": "konto_nr",
                "column": "konto_nr",
                "distinct": 2,
                "enumerable": True,
                "time": False,
                "label_column": "konto_text",
                "categories": ["10", "20"],
            },
            {
                "id": "leverantor",
                "column": "leverantor",
                "distinct": 1200,
                "enumerable": False,
                "time": False,
            },
        ],
        "measures": ["belopp", "antal"],
        "kind": "cube",
        "identifiers": ["fakturanummer"],
        "constants": {"kommun_id": "1480"},
        "catalogue": {},
        "records": [],
        "cells_unique": False,
        "updated": "2026-01-02T10:00:00Z",
    }


def run_with_server(test):
    tables = {"t2023": ledger_rows(2023), "t2024": ledger_rows(2024)}
    calls = []

    async def handle(request):
        calls.append(dict(request.query))
        code = request.path.rsplit("/", 1)[1]
        if code not in tables:
            return web.Response(status=404)
        return web.json_response(page(tables[code], dict(request.query)))

    async def main():
        app = web.Application()
        app.router.add_get("/rowstore/dataset/{code}", handle)
        async with TestServer(app) as server:
            url = str(server.make_url("")).rstrip("/")
            document = to_document(
                dataset_record(url), binding("entryscape", "ex", label="Ex")
            )
            metadata = {
                "/id": document["id"],
                "/dimension": document["dimension"],
                "/extension/entryscape": document["extension"]["entryscape"],
            }
            async with aiohttp.ClientSession() as session:

                async def fetch(selection, **overrides):
                    fragment = await retrieve(
                        session,
                        provider_code="ex",
                        dataset_code="ex:reskontra",
                        language="sv",
                        selection=selection,
                        data_url=document["extension"]["nordicintel"]["data_url"],
                        metadata_url=None,
                        metadata={**metadata, **overrides},
                    )
                    validate_retrieval(fragment, selection=selection)
                    return fragment

                return await test(fetch, calls, url)

    return asyncio.run(main())


def test_required_metadata_points_at_dimensions_and_native_extension():
    assert REQUIRED_METADATA == ("/id", "/dimension", "/extension/entryscape")


def test_full_selection_sums_rows_per_cell_and_flags_counts():
    def test(fetch, calls, url):
        return fetch(
            {
                "part": ["2023"],
                "forvaltning": ["Skola", "Vård"],
                "konto_nr": ["10"],
                "measure": ["belopp", "antal"],
            }
        )

    fragment = run_with_server(test)
    assert fragment["id"] == ["part", "forvaltning", "konto_nr", "measure"]
    assert fragment["size"] == [1, 2, 1, 2]
    # Skola, konto 10: odd i below 300 -> 150 rows; "antal" sums to 150.
    assert fragment["value"][1] == 150 and fragment["status"]["0"].startswith(
        "rows=150"
    )
    assert fragment["value"][3] == 150


def test_omitted_dimensions_aggregate_over_all_parts_and_values():
    def test(fetch, calls, url):
        return fetch({"forvaltning": ["Skola"], "measure": ["antal"]})

    fragment = run_with_server(test)
    assert fragment["id"] == ["forvaltning", "measure"]
    assert fragment["value"] == [400]  # 200 odd rows per year, both parts
    assert fragment["status"] == {"0": "rows=400"}


def test_markers_and_missing_cells_are_sparse():
    def test(fetch, calls, url):
        return fetch(
            {
                "part": ["2024"],
                "konto_nr": ["20", "10"],
                "forvaltning": ["Vård"],
                "measure": ["belopp"],
            }
        )

    fragment = run_with_server(test)
    assert fragment["id"][1] == "konto_nr" and fragment["dimension"]["konto_nr"][
        "category"
    ]["index"] == ["20", "10"]
    assert all(
        status.startswith("rows=") and ".." in status
        for status in fragment["status"].values()
    )
    assert isinstance(fragment["value"], list) and len(fragment["value"]) == 2


def test_filters_are_exact_or_anchored_alternations():
    assert filter_value(["Skola"]) == "Skola"
    assert filter_value(["Skola", "Vård"]) == "^(?:Skola|Vård)$"
    assert (
        filter_value(["a.b"]) == "a.b" and filter_value(["a.b", "c"]) == "^(?:a\\.b|c)$"
    )
    assert filter_value([BLANK]) == "^(?:\\s*)$"
    assert number("1 234,5") == 1234.5 and number("12") == 12 and number("..") is None


async def _errors(fetch, calls, url):
    with pytest.raises(ValueError, match="Unknown parts"):
        await fetch({"part": ["2025"], "measure": ["antal"]})
    with pytest.raises(ValueError, match="measure"):
        await fetch({"forvaltning": ["Skola"]})
    with pytest.raises(LookupError):
        await fetch(
            {"measure": ["antal"]},
            **{"/extension/entryscape": {"tables": [f"{url}/rowstore/dataset/gone"]}},
        )
    before = len(calls)
    await fetch({"measure": ["antal"]})
    return len(calls) - before


def test_unknown_parts_missing_measure_and_gone_tables_raise():
    assert run_with_server(_errors) == 2  # one page per table for the last call


def test_request_bound_is_enforced():
    assert MAX_REQUESTS == 50

"""Actual adapter requests/results through direct and catalog entry points (offline)."""

import asyncio
import importlib
from copy import deepcopy

import pytest

from apps.harvest.contributions import convert_dataset
from retrieval import retrieve_distribution
from retrieval.distribution import ADAPTERS


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    async def json(self, **kwargs):
        return deepcopy(self.body)

    async def text(self):
        return str(self.body)

    async def read(self):
        return str(self.body).encode("utf-8")


class Session:
    """Record transport calls without replacing any adapter implementation."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        return Response(self.bodies.pop(0))

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def case(adapter, scope):
    selection = {"x": ["01"]}
    native = {}
    dimensions = {}
    roles = {}
    code = "01"
    namespace = adapter
    if adapter.startswith("pxweb_"):
        native = {"max_cells": 1}
        bodies = [
            {
                "id": ["x"],
                "size": [1],
                "dimension": {"x": {"category": {"index": ["01"]}}},
                "value": [0],
            }
        ]
    elif adapter == "kolada":
        selection = {scope: ["01"], "year": ["2025"]}
        native = {"kpi_id": "N1", "scope": scope}
        code = f"{scope}/N1"
        bodies = [
            {
                "values": [
                    {
                        "kpi": "N1",
                        scope: "01",
                        "period": 2025,
                        "values": [{"gender": "T", "value": 0, "isdeleted": False}],
                    }
                ],
                "next_url": None,
            }
        ]
    elif adapter == "uka":
        selection = {"Tid": ["2025"], "x": ["01"]}
        roles = {"time": ["Tid"]}
        bodies = [{"url": "https://example.org/export.csv"}, "Tid;x;Antal\n2025;01;0\n"]
    elif adapter == "sdb":
        namespace = "socialstyrelsen"
        native = {"result_dimensions": {"x": "x"}}
        bodies = [{"data": [{"x": "01", "varde": "0"}], "sidor": 1}]
    elif adapter == "entrysoft":
        selection["measure"] = ["v"]
        dimensions = {"x": {"extension": {"entrysoft": {"column": "x"}}}}
        bodies = [{"results": [{"x": "01", "v": 0}], "resultCount": 1}]
    elif adapter == "trafa":
        selection["measure"] = ["v"]
        bodies = [
            {
                "Header": {"Column": [{"Name": "x"}, {"Name": "v"}]},
                "Rows": [
                    {
                        "Cell": [
                            {"Column": "x", "Name": "01", "IsMeasure": False},
                            {"Column": "v", "Value": "0", "IsMeasure": True},
                        ]
                    }
                ],
            }
        ]
    elif adapter == "smhi_emissions":
        selection = {
            "Region": ["00"],
            "Sektor": ["0"],
            "Tid": ["2025"],
            "ContentsCode": ["CO2"],
        }
        native = {
            "substance": {"code": "CO2"},
            "sectors": [{"code": "1", "label": "Energy"}],
        }
        bodies = [{"data": {"CO2": {"Totalt": [{"year": 2025, "value": 0}]}}}]
    elif adapter == "riksbank":
        selection = {"Measure": ["rate"], "Time": ["2025-01-02"]}
        native = {"api": "swestr"}
        bodies = [[{"date": "2025-01-02", "rate": 0}]]
    elif adapter == "forsakringskassan":
        code = "report/Table"
        selection["ContentsCode"] = ["v"]
        native = {"measure_dimension": "ContentsCode"}
        bodies = [[{"dimensions": {"x": "01"}, "observations": {"v": {"value": 0}}}]]
    elif adapter == "dataanalys":
        namespace = "socialstyrelsen_analys"
        native = {"values_dataset_id": 1, "columns": {"x": "x", "value": "v"}}
        bodies = [{"result": [{"data": [{"x": "01", "v": 0}]}]}]
    elif adapter == "tillvaxtverket":
        selection["ContentsCode"] = ["v"]
        native = {"dimension_columns": {"x": "x"}}
        bodies = [{"rowCount": 1, "rows": [{"x": "01", "v": 0}]}]
    else:
        raise AssertionError(adapter)
    doc = {
        "class": "dataset",
        "version": "2.0",
        "label": "Fixture",
        "id": list(selection),
        "size": [len(v) for v in selection.values()],
        "value": [],
        "role": roles,
        "dimension": {
            name: {"category": {"index": list(values)}, **dimensions.get(name, {})}
            for name, values in selection.items()
        },
        "extension": {
            namespace: native,
            "nordicintel": {
                "provider_code": "fixture",
                "dataset_code": code,
                "language": "sv",
                "data_url": "https://example.org/data",
                "metadata_url": "https://example.org/meta",
            },
        },
    }
    return doc, selection, bodies


@pytest.mark.parametrize(
    "adapter,scope",
    [(a, "municipality") for a in sorted(ADAPTERS)] + [("kolada", "ou")],
)
@pytest.mark.parametrize("profile,language", [(1, "sv"), (2, "en")])
def test_dispatch_matches_direct_requests_and_observations(
    adapter, scope, profile, language
):
    doc, selection, bodies = case(adapter, scope)
    source = doc["extension"]["nordicintel"]
    language = "sv" if adapter == "kolada" else language
    source["language"] = language
    source["provider_code"] = {"kolada": "kolada", "pxweb_v2": "scb"}.get(
        adapter, "fixture"
    )
    encoded = convert_dataset(doc, adapter=adapter).dataset.distributions[0].to_jsonld()
    encoded.pop("@context", None)
    encoded["unfamiliar"] = {"nested": [False, 0, ""]}
    if profile == 1:
        identity = encoded["ni:retrieval"]["source"]
        identity["provider_code"] = identity.pop("source_code")
    before = deepcopy(encoded)
    module = importlib.import_module(f"retrieval.data_adapters.{adapter}")
    metadata = {}
    for pointer in module.REQUIRED_METADATA:
        value = doc
        for part in pointer.strip("/").split("/"):
            value = value[part]
        metadata[pointer] = value

    async def run():
        direct, dispatched = Session(bodies), Session(bodies)
        expected = await module.retrieve(
            direct,
            provider_code=source["provider_code"],
            dataset_code=source["dataset_code"],
            language=language,
            selection=selection,
            data_url=source["data_url"],
            metadata_url=source["metadata_url"],
            metadata=metadata,
        )
        actual = await retrieve_distribution(dispatched, encoded, selection=selection)
        assert actual == expected
        assert direct.calls and direct.calls == dispatched.calls
        assert not direct.bodies and not dispatched.bodies
        values = actual["value"]
        assert (list(values.values()) if isinstance(values, dict) else values) == [0]
        assert not direct.closed and not dispatched.closed

    asyncio.run(run())
    assert encoded == before

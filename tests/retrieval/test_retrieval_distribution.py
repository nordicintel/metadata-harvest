"""Catalog projection parity, validation and installed-library dependency seam."""

import asyncio
import importlib
from copy import deepcopy

import pytest

from apps.harvest.contributions import convert_dataset, legacy_document
from retrieval import retrieve_distribution
from retrieval.distribution import ADAPTERS
from tests.model.test_statdcat import legacy


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
@pytest.mark.parametrize("language,profile", [("sv", 1), ("en", 2)])
def test_catalog_projection_matches_previous_adapter_inputs(
    adapter, language, profile, monkeypatch
):
    doc = legacy(language=language)
    doc["extension"][adapter] = doc["extension"].pop("pxweb_v2")
    doc["extension"]["socialstyrelsen"] = {"result_dimensions": {"Region": "region"}}
    doc["extension"]["socialstyrelsen_analys"] = {"dataset": "01", "false": False}
    doc["extension"]["smhi"] = {"future": 0}
    model = convert_dataset(doc, adapter=adapter).dataset.distributions[0]
    encoded = model.to_jsonld()
    encoded["unfamiliar"] = {"nested": [False, 0, ""]}
    if profile == 1:
        source = encoded["ni:retrieval"]["source"]
        source["provider_code"] = source.pop("source_code")
    else:
        encoded.pop("@context")
    before = deepcopy(encoded)
    projected = legacy_document(model)
    module = importlib.import_module(f"retrieval.data_adapters.{adapter}")
    expected = {}
    for pointer in module.REQUIRED_METADATA:
        value = projected
        for part in pointer.strip("/").split("/"):
            value = value[part]
        expected[pointer] = value
    called = []
    result = {"id": ["Region", "Tid"], "size": [1, 1], "value": [0]}
    session = object()

    async def capture(actual_session, **kwargs):
        assert actual_session is session
        called.append(kwargs)
        return result

    monkeypatch.setattr(module, "retrieve", capture)
    selected = {"Region": ["00"], "Tid": ["2021"]}
    assert (
        asyncio.run(retrieve_distribution(session, encoded, selection=selected))
        is result
    )
    assert called == [
        {
            "provider_code": model.retrieval.source.source_code,
            "dataset_code": model.retrieval.source.dataset_code,
            "language": language,
            "selection": selected,
            "data_url": model.access_url[0],
            "metadata_url": model.retrieval.metadata_url,
            "metadata": expected,
        }
    ]
    assert encoded == before


@pytest.mark.parametrize(
    "damage",
    ["adapter", "binding", "url", "order", "duplicate", "elimination", "categories"],
)
def test_invalid_operational_structure_fails_before_requests(damage):
    encoded = (
        convert_dataset(legacy(), adapter="pxweb_v2")
        .dataset.distributions[0]
        .to_jsonld()
    )
    binding = encoded["ni:retrieval"]
    dimension = encoded["ni:structure"]["qb:component"][0]["qb:dimension"]
    if damage == "adapter":
        binding["source"]["adapter"] = "unknown"
    elif damage == "binding":
        del encoded["ni:retrieval"]
    elif damage == "url":
        encoded["dcat:accessURL"] = ["file:///tmp/data"]
    elif damage == "order":
        encoded["ni:structure"]["qb:component"][0]["qb:order"] = 2
    elif damage == "duplicate":
        binding["dimensions"][0]["categories"] = ["01", "01"]
    elif damage == "elimination":
        dimension["ni:elimination"] = "false"
    elif damage == "categories":
        dimension["qb:codeList"]["codes"].pop()
    with pytest.raises(ValueError):
        asyncio.run(
            retrieve_distribution(object(), encoded, selection={"Region": ["00"]})
        )


@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"Region": "00"},
        {"Region": ["00", "00"]},
        {"Region": ["missing"]},
        {"unknown": ["00"]},
        {"Region": [0]},
    ],
)
def test_invalid_selection_fails_before_requests(selection):
    encoded = (
        convert_dataset(legacy(), adapter="pxweb_v2")
        .dataset.distributions[0]
        .to_jsonld()
    )
    with pytest.raises(ValueError):
        asyncio.run(retrieve_distribution(object(), encoded, selection=selection))


def test_harvesting_exposes_no_consumer_retrieval():
    import apps.harvest

    assert not hasattr(apps.harvest, "retrieve_distribution")
    for adapter in ADAPTERS:
        module = importlib.import_module(f"apps.harvest.metadata_adapters.{adapter}")
        assert not hasattr(module, "retrieve")
        assert (
            importlib.util.find_spec(
                f"apps.harvest.metadata_adapters.{adapter}.retrieval"
            )
            is None
        )

"""Worker documents drive retrieval dispatch; validation happens before any I/O."""

import asyncio
import importlib
from copy import deepcopy

import pytest

from retrieval import retrieve_distribution
from retrieval.distribution import ADAPTERS
from tests.support.worker_document import legacy


def document(adapter="pxweb_v2", language="sv"):
    doc = legacy(language=language)
    doc["extension"][adapter] = doc["extension"].pop("pxweb_v2")
    doc["extension"]["nordicintel"]["adapter"] = adapter
    doc["extension"]["socialstyrelsen"] = {"result_dimensions": {"Region": "region"}}
    doc["extension"]["socialstyrelsen_analys"] = {"dataset": "01", "false": False}
    doc["extension"]["smhi"] = {"future": 0}
    return doc


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
@pytest.mark.parametrize("language", ["sv", "en"])
def test_dispatch_passes_document_fields_and_required_metadata(
    adapter, language, monkeypatch
):
    doc = document(adapter, language)
    doc["unfamiliar"] = {"nested": [False, 0, ""]}
    before = deepcopy(doc)
    module = importlib.import_module(f"retrieval.data_adapters.{adapter}")
    expected = {}
    for pointer in module.REQUIRED_METADATA:
        value = doc
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
        asyncio.run(retrieve_distribution(session, doc, selection=selected)) is result
    )
    assert called == [
        {
            "provider_code": "scb",
            "dataset_code": "T1",
            "language": language,
            "selection": selected,
            "data_url": "https://example.org/data",
            "metadata_url": "https://example.org/meta",
            "metadata": expected,
        }
    ]
    assert doc == before


@pytest.mark.parametrize(
    "damage",
    ["adapter", "shared", "url", "size", "duplicate", "elimination", "categories"],
)
def test_invalid_operational_structure_fails_before_requests(damage):
    doc = document()
    shared = doc["extension"]["nordicintel"]
    region = doc["dimension"]["Region"]
    if damage == "adapter":
        shared["adapter"] = "unknown"
    elif damage == "shared":
        del doc["extension"]["nordicintel"]
    elif damage == "url":
        shared["data_url"] = "file:///tmp/data"
    elif damage == "size":
        doc["size"] = [3, 2]
    elif damage == "duplicate":
        region["category"]["index"] = {"01": 0, "00": 0}
    elif damage == "elimination":
        region["extension"]["nordicintel"]["elimination_value"] = "missing"
    elif damage == "categories":
        region["category"]["label"]["02"] = "Unknown"
    with pytest.raises(ValueError):
        asyncio.run(retrieve_distribution(object(), doc, selection={"Region": ["00"]}))


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
    with pytest.raises(ValueError):
        asyncio.run(retrieve_distribution(object(), document(), selection=selection))


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

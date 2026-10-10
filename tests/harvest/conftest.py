"""Harvesting tests must remain independent of consumer observation retrieval."""

import importlib

import pytest

from retrieval.distribution import ADAPTERS


@pytest.fixture(autouse=True)
def independent_harvesting(request, monkeypatch):
    """Fail if a harvesting client/source/workflow delegates to consumer retrieval."""

    async def forbidden(*args, **kwargs):
        pytest.fail("Harvesting called consumer observation retrieval")

    for adapter in ADAPTERS:
        module = importlib.import_module(f"retrieval.data_adapters.{adapter}")
        monkeypatch.setattr(module, "retrieve", forbidden)
    for module_name in ("retrieval", "retrieval.distribution"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "retrieve_distribution", forbidden)

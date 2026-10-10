"""Harvesting tests must remain independent of consumer observation retrieval."""

import importlib

import pytest

from retrieval.distribution import ADAPTERS


@pytest.fixture(autouse=True)
def independent_harvesting(request, monkeypatch):
    """Fail if a harvesting client/source/workflow delegates to consumer retrieval."""
    name = request.path.stem
    if not any(part in name for part in ("workflow", "client", "source", "metadata")):
        return

    async def forbidden(*args, **kwargs):
        pytest.fail("Harvesting called consumer observation retrieval")

    for adapter in ADAPTERS:
        module = importlib.import_module(f"retrieval.adapters.{adapter}")
        monkeypatch.setattr(module, "retrieve", forbidden)
    for module_name in ("retrieval", "retrieval.distribution", "harvesting.retrieval"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "retrieve_distribution", forbidden)

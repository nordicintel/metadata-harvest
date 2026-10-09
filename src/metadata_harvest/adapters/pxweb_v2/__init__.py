"""SCB PXWeb v2; harvesting imports load only when requested."""

from importlib import import_module

__all__ = [
    "Client",
    "discover_provider",
    "fetch_codelist",
    "fetch_metadata",
    "retrieve",
]


def __getattr__(name):
    modules = {
        "Client": "client",
        "discover_provider": "discovery",
        "fetch_metadata": "metadata",
        "fetch_codelist": "metadata",
        "retrieve": "retrieval",
    }
    if name not in modules:
        raise AttributeError(name)
    return getattr(import_module(f"{__name__}.{modules[name]}"), name)

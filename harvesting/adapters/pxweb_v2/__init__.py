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


async def fetch_metadata(record, *, client=None):
    """Build validated source contributions through the shared profile boundary."""
    from harvesting.accepted import as_contributions
    from harvesting.adapters.pxweb_v2.metadata import fetch_metadata as fetch

    return as_contributions(await fetch(record, client=client), adapter="pxweb_v2")


async def fetch_codelist(code, language, *, client=None):
    """Normalize a native SCB codelist as a typed SKOS concept scheme."""
    from harvesting.adapters.pxweb_v2.metadata import fetch_codelist as fetch
    from harvesting.statdcat import convert_codelist

    body = await fetch(code, language, client=client)
    return convert_codelist(
        {"provider_code": "scb", "language": language, "codelist": body}
    )

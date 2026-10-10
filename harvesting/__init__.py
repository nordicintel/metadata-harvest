"""Repository harvesting and typed bridges to standalone retrieval."""

__all__ = [
    "get_pxweb_metadata",
    "get_pxweb_tables",
    "retrieve_distribution",
    "Dataset",
    "Distribution",
    "CodeList",
    "Organisation",
    "Catalog",
    "DataService",
    "AdapterDefinition",
    "CatalogConfiguration",
    "ServiceBinding",
]


def __getattr__(name):
    if name == "retrieve_distribution":
        from harvesting.retrieval import retrieve_distribution

        return retrieve_distribution
    if name in {"AdapterDefinition", "CatalogConfiguration", "ServiceBinding"}:
        from harvesting import configuration

        return getattr(configuration, name)
    if name in {
        "Dataset",
        "Distribution",
        "CodeList",
        "Organisation",
        "Catalog",
        "DataService",
    }:
        from harvesting import models

        return getattr(models, name)
    if name == "get_pxweb_tables":
        from harvesting.adapters.pxweb_v1.discovery import get_pxweb_tables

        return get_pxweb_tables
    if name == "get_pxweb_metadata":
        from harvesting.adapters.pxweb_v1.metadata import get_pxweb_metadata

        return get_pxweb_metadata
    raise AttributeError(name)

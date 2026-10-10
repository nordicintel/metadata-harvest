"""Repository harvesting and metadata models."""

__all__ = [
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
    raise AttributeError(name)

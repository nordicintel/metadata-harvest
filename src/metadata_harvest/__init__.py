"""Metadata harvesting and standalone live retrieval."""

__all__ = [
    "get_pxweb_metadata",
    "get_pxweb_tables",
    "retrieve_distribution",
    "Dataset",
    "Distribution",
    "CodeList",
]


def __getattr__(name):
    if name == "retrieve_distribution":
        from metadata_harvest.retrieval import retrieve_distribution

        return retrieve_distribution
    if name in {"Dataset", "Distribution", "CodeList"}:
        from metadata_harvest import models

        return getattr(models, name)
    if name == "get_pxweb_tables":
        from metadata_harvest.adapters.pxweb_v1.discovery import get_pxweb_tables

        return get_pxweb_tables
    if name == "get_pxweb_metadata":
        from metadata_harvest.adapters.pxweb_v1.metadata import get_pxweb_metadata

        return get_pxweb_metadata
    raise AttributeError(name)

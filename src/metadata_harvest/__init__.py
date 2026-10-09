"""Metadata harvesting and standalone live retrieval."""

__all__ = ["get_pxweb_metadata", "get_pxweb_tables"]


def __getattr__(name):
    if name == "get_pxweb_tables":
        from metadata_harvest.adapters.pxweb_v1.discovery import get_pxweb_tables

        return get_pxweb_tables
    if name == "get_pxweb_metadata":
        from metadata_harvest.adapters.pxweb_v1.metadata import get_pxweb_metadata

        return get_pxweb_metadata
    raise AttributeError(name)

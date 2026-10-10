"""Tillväxtverket and data.europa.eu URLs for harvesting; shared constants live in
the retrieval module."""

from urllib.parse import urlencode

from retrieval.adapters.tillvaxtverket import (
    BASE_URL,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    REGION_DIMENSION,
    cbase_url,
)

__all__ = [
    "API_DOCUMENTATION_URL",
    "BASE_URL",
    "DCAT_API",
    "DCAT_PAGE",
    "MEASURE_DIMENSION",
    "PROVIDER_CODE",
    "REGION_DIMENSION",
    "cbase_url",
    "data_url",
    "dcat_id",
    "dcat_page_url",
    "dcat_url",
    "labels_url",
    "metadata_url",
]

API_DOCUMENTATION_URL = f"{BASE_URL}/api.yaml"
"""DivePort Web API description (OpenAPI), linked from every DCAT JSON distribution."""
DCAT_API = "https://data.europa.eu/api/hub/search/datasets"
DCAT_PAGE = "https://data.europa.eu/data/datasets"


def data_url(area: str, name: str) -> str:
    """The observation endpoint, as published in the DCAT JSON distribution."""
    return f"{cbase_url(area, name)}?{urlencode({'format': 'json'})}"


def metadata_url(area: str, name: str) -> str:
    """Columns and ``rowCount`` without rows."""
    return f"{cbase_url(area, name)}?{urlencode({'format': 'json', 'limit': 0})}"


def labels_url(area: str, name: str) -> str:
    """Tillväxtverket's column label file (``Name;Type;Length;Label``, Latin-1)."""
    return f"{BASE_URL}/data/{area}/{name}_variabeletiketter.csv"


def dcat_id(name: str) -> str:
    """The data.europa.eu dataset id harvested from Sveriges dataportal."""
    return f"https-oppnadata-tillvaxtverket-se-dataset_{name.lower()}"


def dcat_url(name: str) -> str:
    return f"{DCAT_API}/{dcat_id(name)}"


def dcat_page_url(name: str) -> str:
    return f"{DCAT_PAGE}/{dcat_id(name)}"

"""Trafikanalys URLs for harvesting; shared constants live in the retrieval module."""

from urllib.parse import urlencode

from retrieval.data_adapters.trafa import (
    ACCEPT_JSON,
    API_ROOT,
    DATA_URL,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    api_language,
)

__all__ = [
    "ACCEPT_JSON",
    "API_ROOT",
    "DATA_URL",
    "DOCUMENTATION_URL",
    "MEASURE_DIMENSION",
    "PROVIDER_CODE",
    "STRUCTURE_URL",
    "api_language",
    "documentation_url",
    "structure_url",
]

STRUCTURE_URL = f"{API_ROOT}/structure"
DOCUMENTATION_URL = "https://www.trafa.se/sidor/api-dokumentation/"


def structure_url(product: str, language: str) -> str:
    """Return the ``/structure`` URL describing one product in ``language``."""
    return f"{STRUCTURE_URL}?{urlencode({'query': product, 'lang': api_language(language)})}"


def documentation_url(product_id: str) -> str:
    """Return the per-product API documentation page for a product ``UniqueId``."""
    return f"{DOCUMENTATION_URL}?{urlencode({'code': product_id})}"

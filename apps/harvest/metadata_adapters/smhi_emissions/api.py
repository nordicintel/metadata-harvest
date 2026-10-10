"""Nationella emissionsdatabasen URLs; shared constants live in the retrieval module."""

from retrieval.data_adapters.smhi_emissions import (
    API_ROOT,
    DATA_URL,
    FILTER_OPTIONS_URL,
    NAMESPACE,
    NATIONAL,
    PROVIDER_CODE,
    TOTAL,
    data_parameters,
    dataset_code,
)

__all__ = [
    "API_ROOT",
    "DATA_URL",
    "DOC_URL",
    "FILTER_OPTIONS_URL",
    "NAMESPACE",
    "NATIONAL",
    "PROVIDER_CODE",
    "SOURCE_URL",
    "TOTAL",
    "data_parameters",
    "dataset_code",
    "probe_parameters",
]

SOURCE_URL = "https://nationellaemissionsdatabasen.smhi.se/"
"""The public map and chart application; there is no page per substance."""
DOC_URL = "https://www.smhi.se/data/luftkvalitet/nationella-emissionsdatabasen"
"""SMHI's information page (method, user manual, contact address)."""


def probe_parameters(substance: str) -> dict[str, str]:
    """The national ``hsektor=0`` query: Totalt and main sectors for every year."""
    return data_parameters(NATIONAL, TOTAL, substance)

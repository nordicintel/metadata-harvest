"""Försäkringskassan URLs for harvesting; shared constants live in the retrieval module."""

from urllib.parse import quote

from retrieval.data_adapters.forsakringskassan import (
    API_ROOT,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    ROW_COUNTER,
    SUPPRESSED,
    meta_url,
    table_url,
)

__all__ = [
    "API_ROOT",
    "MEASURE_DIMENSION",
    "PROVIDER_CODE",
    "REPORTS_URL",
    "ROW_COUNTER",
    "SITE",
    "SUPPRESSED",
    "TEXTS_URL",
    "meta_url",
    "source_url",
    "table_url",
]

SITE = "https://www.forsakringskassan.se"
REPORTS_URL = f"{API_ROOT}/reports"
"""The catalogue: benefit groups (``key``) in areas, each with its report keys."""
TEXTS_URL = f"{SITE}/rest-api/fk-rest-textnycklar/public/v2/meka?lang=sv-se"
"""Swedish text keys of the statistics database: report names, ingresses and
descriptions (``MEKA.report.{report}``), benefits (``MEKA.forman.{forman}``) and
areas (``MEKA.area.{area}``). ``lang=en`` answers the same Swedish texts."""
STATISTICS_PAGE = f"{SITE}/statistik-och-analys/statistikdatabas"


def source_url(area: str, report: str) -> str:
    """Return the report's page in the web statistics database."""
    return f"{STATISTICS_PAGE}#!/{quote(area, safe='')}/{quote(report, safe='')}"

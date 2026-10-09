"""Analysverktyg URLs, list queries and request bodies for harvesting.

Shared constants live in the retrieval module, which must not import harvesting code.
"""

from urllib.parse import quote

from metadata_harvest.adapters.dataanalys.retrieval import (
    ACCEPT_JSON,
    AGE,
    API_ROOT,
    DATA_URL,
    MEASURE,
    PROVIDER_CODE,
    REGION,
    SEX,
    SITE,
    TIME,
    missing_columns,
    query_body,
    result_rows,
)

__all__ = [
    "ACCEPT_JSON",
    "AGE",
    "API_ROOT",
    "CATALOGUE_OPTIONAL",
    "CATALOGUE_REQUIRED",
    "CATALOGUE_ROW_LIMIT",
    "CHART_COLUMNS",
    "CHART_LIST_URL",
    "CUBE_COLUMNS",
    "CUBE_ROW_LIMIT",
    "DASHBOARD_COLUMNS",
    "DASHBOARD_LIST_URL",
    "DATA_URL",
    "LEVELS",
    "MEASURE",
    "PAGE_SIZE",
    "PROVIDER_CODE",
    "REGION",
    "SEX",
    "SITE",
    "TIME",
    "TRENDER_COLUMNS",
    "catalogue_body",
    "cube_body",
    "dashboard_datasets_url",
    "dashboard_url",
    "list_query",
    "missing_columns",
    "probe_body",
    "query_body",
    "result_rows",
]

CHART_LIST_URL = f"{API_ROOT}/chart/"
DASHBOARD_LIST_URL = f"{API_ROOT}/dashboard/"
PAGE_SIZE = 100
"""Superset's largest list page."""
CHART_COLUMNS = (
    "id",
    "slice_name",
    "datasource_id",
    "datasource_name_text",
    "form_data",
    "dashboards.id",
)
DASHBOARD_COLUMNS = (
    "id",
    "slug",
    "url",
    "dashboard_title",
    "published",
    "changed_on_utc",
    "tags.name",
)

CATALOGUE_REQUIRED = ("MBusinessKey", "HrefIndicatorDashboard", "DataGenerationDate")
CATALOGUE_OPTIONAL = (
    "MTypeName",
    "MName",
    "Unit",
    "AreaName",
    "LinkIndicatorDescription",
    "LinkIndicatorDescriptionTitle",
    "DataSource",
    "Description",
    "DesiredValue",
    "InputType",
    "Keywords",
    "MeasurePeriodInterval",
)
"""Catalogue columns dropped when an area's table lacks them (LRHST has no
``MTypeName``, NPS no ``Unit``)."""
CATALOGUE_ROW_LIMIT = 1000
CUBE_ROW_LIMIT = 20_000
LEVELS = ("Riket", "Region")
"""Geographic levels harvested; kommun and care-unit levels are out of scope."""

TRENDER_COLUMNS = {
    # logical name: (candidate source columns in order of preference, required)
    "indicator": (("Matt_id", "MBusinessKey"), True),
    "level": (("Indelning_typ", "GroupTypeName"), True),
    "code": (("Indelning_etikett", "GroupName"), True),
    "sex": (("Kon", "Gender", "Kön"), False),
    "age": (("Alder", "AgeLabel", "Ålder", "Age"), False),
    "period_type": (("Matperiod_typ", "Measurementperiodtype"), False),
    "period": (("Matperiod_char", "MeasurementPeriod", "Mätperiod"), True),
    "period_date": (("Matperiod_datum", "MeasurementPeriodDate"), False),
    "value": (("Varde", "Value", "Värde"), True),
    "status": (("Kommentar", "Commentary"), False),
}
"""Logical ``Trender`` columns. Each area's values table aliases the same source
columns differently (live 2026-10-09): GONV uses ``Matt_id``/``Indelning_*``/
``Matperiod_*``/``Varde``; NRAA, LRHST, OB and NRTV the native names
(``MBusinessKey``, ``GroupName``, ``MeasurementPeriod``, ``Value``); PS Swedish
aliases with diacritics (``Mätperiod``, ``Värde``, ``Kön``, ``Ålder``). A one-row
probe per area picks the first candidate the table has."""
CUBE_COLUMNS = (
    "level",
    "code",
    "sex",
    "age",
    "period_type",
    "period",
    "period_date",
)
"""Logical columns of the per-indicator dimension query, in request order."""


def list_query(columns, page: int) -> str:
    """The rison ``q`` of one list page."""
    return f"(columns:!({','.join(columns)}),page:{page},page_size:{PAGE_SIZE})"


def dashboard_url(slug: str) -> str:
    """The human-readable dashboard page."""
    return f"{SITE}/superset/dashboard/{quote(slug, safe='')}/"


def dashboard_datasets_url(dashboard_id: int) -> str:
    """The datasets (tables and column labels) behind a dashboard."""
    return f"{API_ROOT}/dashboard/{dashboard_id}/datasets"


def catalogue_body(datasource_id: int, columns) -> dict:
    """One row per indicator: the area's catalogue at the ``Riket`` level."""
    return query_body(
        datasource_id,
        columns,
        [{"col": "GroupTypeName", "op": "IN", "val": ["Riket"]}],
        CATALOGUE_ROW_LIMIT,
    )


def probe_body(datasource_id: int, columns) -> dict:
    """One row of ``columns``, to learn which of them the ``Trender`` table has."""
    return query_body(datasource_id, columns, [], 1)


def cube_body(datasource_id: int, columns: dict[str, str], indicator: str) -> dict:
    """The distinct dimension rows of one indicator at the harvested levels."""
    return query_body(
        datasource_id,
        [columns[name] for name in CUBE_COLUMNS if name in columns],
        [
            {"col": columns["indicator"], "op": "IN", "val": [indicator]},
            {"col": columns["level"], "op": "IN", "val": list(LEVELS)},
        ],
        CUBE_ROW_LIMIT,
    )

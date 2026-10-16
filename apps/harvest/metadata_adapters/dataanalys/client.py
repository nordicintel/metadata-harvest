"""Paced, retrying JSON client for harvesting from the analysverktyg (Superset)."""

import asyncio
import logging
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from harvest.lifecycle import Unreachable as _Unreachable
from retrieval.data_adapters.dataanalys import (
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

logger = logging.getLogger("dataanalys_logger")

TIMEOUT = aiohttp.ClientTimeout(total=120, sock_connect=30, sock_read=90)
USER_AGENT = "nordicintel-metadata-harvest/dataanalys"
MAX_RETRY_AFTER = 300.0

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


class Unreachable(_Unreachable):
    """The analysverktyg did not answer a request, after every retry."""


class RequestRejected(Exception):
    """The analysverktyg answered a request with a client error (4xx except 429)."""

    def __init__(self, method: str, url: str, status: int, text: str) -> None:
        super().__init__(f"{method} {url} answered HTTP {status}: {text[:300]}")
        self.status = status
        self.text = text


class _Transient(Exception):
    def __init__(self, status: int, text: str, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta seconds or an HTTP date)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - time.time())


class DataanalysClient:
    """Fetch analysverktyg resources one at a time through one session.

    Requests start at least ``request_interval_seconds`` (provider configuration,
    default 1.0) apart. Timeouts, connection errors, HTTP 429 and 5xx responses are
    retried with exponential backoff, honouring ``Retry-After``; exhausted retries
    raise :class:`Unreachable`. Other HTTP errors raise :class:`RequestRejected`
    with the response text, which carries Superset's error message.
    """

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        attempts: int = 4,
        retry_delay: float = 2.0,
        interval: float | None = None,
    ) -> None:
        self._interval = (
            float((options or {}).get("request_interval_seconds", 1.0))
            if interval is None
            else interval
        )
        self._session = session
        self._owned = session is None
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._last_start: float | None = None
        self.request_count = 0
        logger.info(
            "Client: requests at least %.1fs apart, up to %d attempts each, "
            "backoff from %.1fs",
            self._interval,
            self._attempts,
            self._retry_delay,
        )

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
        return self._session

    async def close(self) -> None:
        if self._owned and self._session is not None:
            await self._session.close()
        logger.info("Client closed after %d requests", self.request_count)

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        """Return the decoded JSON body of ``GET url``."""
        return await self._request("GET", url, params=params)

    async def post_json(self, url: str, body: Mapping[str, Any]) -> Any:
        """Return the decoded JSON body of ``POST url`` with a JSON ``body``."""
        return await self._request("POST", url, body=body)

    async def _pace(self) -> None:
        if self._last_start is not None:
            wait = self._last_start + self._interval - time.monotonic()
            if wait > 0:
                logger.debug("Pacing: waiting %.2fs before the next request", wait)
                await asyncio.sleep(wait)
        self._last_start = time.monotonic()

    async def _request(self, method: str, url: str, *, params=None, body=None) -> Any:
        for attempt in range(1, self._attempts + 1):
            await self._pace()
            started = time.monotonic()
            self.request_count += 1
            try:
                async with self.session.request(
                    method, url, params=params, json=body, headers=ACCEPT_JSON
                ) as response:
                    if response.status == 429 or response.status >= 500:
                        raise _Transient(
                            response.status,
                            await response.text(),
                            _retry_after(response.headers.get("Retry-After")),
                        )
                    if response.status >= 400:
                        logger.debug(
                            "%s %s rejected with HTTP %d (not retried)",
                            method,
                            url,
                            response.status,
                        )
                        raise RequestRejected(
                            method, url, response.status, await response.text()
                        )
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, _Transient) as error:
                if attempt == self._attempts:
                    logger.error(
                        "Giving up on %s %s after %d attempts (%s: %s)",
                        method,
                        url,
                        attempt,
                        type(error).__name__,
                        error,
                    )
                    raise Unreachable(
                        f"{method} {url} {dict(params or {})} failed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                delay = self._retry_delay * 2 ** (attempt - 1)
                retry_after = getattr(error, "retry_after", None)
                if retry_after is not None:
                    delay = max(delay, min(retry_after, MAX_RETRY_AFTER))
                logger.warning(
                    "Attempt %d/%d for %s %s failed (%s: %s); retrying in %.0fs",
                    attempt,
                    self._attempts,
                    method,
                    url,
                    type(error).__name__,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.debug("%s %s (%.1fs)", method, url, time.monotonic() - started)
            return payload
        raise AssertionError("unreachable")

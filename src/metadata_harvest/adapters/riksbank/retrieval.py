"""Live retrieval of Riksbank observations (NordicIntel retrieval contract).

Imports only ``aiohttp`` and the standard library. The service calls
:func:`retrieve` through the ``nordicintel.retrieval`` entry point ``riksbank``.

The Riksbank throttles anonymous clients hard (HTTP 429 with ``Retry-After`` of
45–56 s after about five quick calls). Retrieval never sleeps or retries: requests
are made one at a time through the caller's session, and the caller owns pacing.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import aiohttp

PROVIDER_CODE = "riksbank"
API_ROOT = "https://api.riksbank.se"
SWEA_ROOT = f"{API_ROOT}/swea/v1"
SWEA_OBSERVATIONS = f"{SWEA_ROOT}/Observations"
MPD_ROOT = f"{API_ROOT}/monetary_policy_data/v1"
MPD_FORECASTS = f"{MPD_ROOT}/forecasts"
MPD_SERIES_IDS = f"{MPD_FORECASTS}/series_ids"
SWESTR_ROOT = f"{API_ROOT}/swestr/v1"
SWESTR_ID = "SWESTR"
ACCEPT_JSON = {"Accept": "application/json"}

SERIES = "Series"
"""Metric dimension of SWEA group and forecast datasets: series ids."""
TIME = "Time"
"""Time dimension: ISO dates (SWEA, SWESTR) or period-end dates (forecasts)."""
POLICY_ROUND = "PolicyRound"
"""Forecast vintage dimension: policy round names such as ``2026:3``."""
MEASURE = "Measure"
"""Metric dimension of the SWESTR dataset: fields of a SWESTR publication."""
SWESTR_MEASURES = (
    "rate",
    "pctl12_5",
    "pctl87_5",
    "volume",
    "numberOfTransactions",
    "numberOfAgents",
)

DIMENSIONS = {
    "swea": (SERIES, TIME),
    "mpd": (SERIES, POLICY_ROUND, TIME),
    "swestr": (MEASURE, TIME),
}
"""Dimension ids of each API's datasets, in document order."""

REQUIRED_METADATA = ("/id", "/extension/riksbank")
"""The dataset's dimension ids, and its ``extension.riksbank``: ``api`` (``swea``,
``mpd`` or ``swestr``) with ``group_id``, ``series_id`` or ``interest_rate_id``."""

MAX_SPAN_DAYS = 46_400
"""Longest date range requested at once from one series. No span limit is
documented for ``/swea/v1/Observations/{seriesId}/{from}/{to}`` or SWESTR history;
1900-01-01 to 2026-10-09 (46,299 days, 32,211 rows, 1 MB) was answered in one call
on 2026-10-09. Longer ranges are split rather than sent untested."""

logger = logging.getLogger(__name__)


def api_of(dataset_code: str) -> str | None:
    """The Riksbank API of a dataset code, or None for an unknown pattern."""
    if dataset_code.startswith("swea_group_"):
        return "swea"
    if dataset_code.startswith("mpd_"):
        return "mpd"
    if dataset_code == "swestr":
        return "swestr"
    return None


def _dates(codes: Sequence[str]) -> list[date]:
    try:
        return [date.fromisoformat(code) for code in codes]
    except ValueError as error:
        raise ValueError(f"{TIME} codes must be ISO dates: {error}") from None


def date_ranges(codes: Sequence[str], max_days: int | None = None) -> list[tuple]:
    """Cover the selected dates with ranges of at most ``max_days`` days.

    Returns ``(first, last)`` date pairs in ascending order; each range starts and
    ends on a selected date, so no request spans only unselected dates.
    """
    limit = max_days or MAX_SPAN_DAYS
    ranges: list[list[date]] = []
    for day in sorted(set(_dates(codes))):
        if ranges and (day - ranges[-1][0]).days < limit:
            ranges[-1][1] = day
        else:
            ranges.append([day, day])
    return [(first, last) for first, last in ranges]


async def _get(
    session: aiohttp.ClientSession,
    url: str,
    params: Mapping[str, str] | None = None,
    *,
    empty: tuple[int, ...] = (204,),
) -> Any:
    """GET ``url`` and decode JSON; None for an ``empty`` status (no content)."""
    async with session.get(url, params=params, headers=ACCEPT_JSON) as response:
        if response.status in empty:
            logger.debug("GET %s %s: %d (no data)", url, params, response.status)
            return None
        if response.status == 400:
            text = (await response.text())[:300]
            raise ValueError(f"Riksbank rejected GET {url} {params or ''}: {text}")
        response.raise_for_status()
        return await response.json(content_type=None)


def _check_selection(api: str, selection, metadata) -> None:
    expected = DIMENSIONS[api]
    if unknown := [name for name in selection if name not in expected]:
        raise ValueError(f"unknown dimensions {unknown}; expected {list(expected)}")
    ids = list(metadata.get("/id") or expected)
    if missing := [name for name in ids if name not in selection]:
        raise ValueError(f"selection must include every dimension; missing {missing}")
    for name in expected:
        if not selection.get(name):
            raise ValueError(f"selection must include {name!r}")


def _swea_series_url(data_url: str | None, series_id: str, first, last) -> str:
    base = (data_url or SWEA_OBSERVATIONS).rstrip("/")
    return f"{base}/{quote(series_id, safe='')}/{first.isoformat()}/{last.isoformat()}"


async def _swea(session, selection, data_url, metadata_url, native) -> dict:
    wanted = set(selection[TIME])
    cells: dict[tuple[str, str], Any] = {}
    empty = []
    for series_id in selection[SERIES]:
        found = False
        for first, last in date_ranges(selection[TIME]):
            rows = await _get(
                session, _swea_series_url(data_url, series_id, first, last)
            )
            for row in rows or []:
                found = True
                day, value = str(row.get("date")), row.get("value")
                if day in wanted and value is not None:
                    cells[(series_id, day)] = value
        if not found:
            empty.append(series_id)
    if empty:
        # SWEA answers 204 both for unknown series and for empty ranges.
        group = native.get("group_id")
        url = metadata_url or (
            f"{SWEA_ROOT}/Series/ByGroup/{group}" if group is not None else None
        )
        if url:
            listed = await _get(session, url)
            if listed is None:
                raise LookupError(f"SWEA group {group} no longer exists ({url})")
            known = {str(row.get("seriesId")) for row in listed}
            if gone := [code for code in empty if code not in known]:
                raise LookupError(f"SWEA series {gone} no longer exist in {url}")
    return {
        (series_id, day): (value, None) for (series_id, day), value in cells.items()
    }


def _vintages(body: Any, series_id: str) -> list[Mapping[str, Any]]:
    """The vintages of ``series_id`` in a ``/forecasts`` response.

    A filtered response holds one vintage as an object; an unfiltered one a list.
    """
    if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
        raise ValueError(f"unexpected forecasts response {str(body)[:200]!r}")
    vintages: list[Mapping[str, Any]] = []
    for entry in body["data"]:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("external_id") or entry.get("series_id")) != series_id:
            continue
        found = entry.get("vintages")
        vintages.extend(found if isinstance(found, list) else [found] if found else [])
    return [v for v in vintages if isinstance(v, Mapping)]


def _split_url(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return urlunsplit((*parts[:3], "", "")), dict(parse_qsl(parts.query))


async def _mpd(session, selection, data_url, metadata_url, native) -> dict:
    base, fixed = _split_url(data_url or MPD_FORECASTS)
    wanted = set(selection[TIME])
    cells: dict[tuple[str, str, str], tuple[Any, None]] = {}
    answered = set()
    for series_id in selection[SERIES]:
        for policy_round in selection[POLICY_ROUND]:
            params = {**fixed, "series": series_id, "policy_round_name": policy_round}
            # 404 means "no vintage": rounds without one (such as 2020:2) and
            # unknown series alike.
            body = await _get(session, base, params, empty=(204, 404))
            if body is None:
                continue
            answered.add(series_id)
            for vintage in _vintages(body, series_id):
                meta = vintage.get("metadata") or {}
                if str(meta.get("policy_round", policy_round)) != policy_round:
                    continue
                for row in vintage.get("observations") or []:
                    period, value = str(row.get("dt")), row.get("value")
                    if period in wanted and value is not None:
                        cells[(series_id, policy_round, period)] = (value, None)
    if missing := [s for s in selection[SERIES] if s not in answered]:
        url = metadata_url or MPD_SERIES_IDS
        listed = await _get(session, url)
        known = {
            str(item.get("series_id"))
            for item in (listed or {}).get("data") or []
            if isinstance(item, Mapping)
        }
        if gone := [code for code in missing if code not in known]:
            raise LookupError(f"forecast series {gone} no longer listed in {url}")
    return cells


def _swestr_status(row: Mapping[str, Any]) -> str | None:
    flags = []
    if row.get("republication"):
        flags.append("republication")
    if row.get("alternativeCalculation"):
        reason = row.get("alternativeCalculationReason")
        flags.append(
            f"alternative calculation: {reason}"
            if reason
            else "alternative calculation"
        )
    return "; ".join(flags) or None


async def _swestr(session, selection, data_url, metadata_url, native) -> dict:
    url = data_url or f"{SWESTR_ROOT}/{native.get('interest_rate_id') or SWESTR_ID}"
    if unknown := [m for m in selection[MEASURE] if m not in SWESTR_MEASURES]:
        raise ValueError(f"unknown SWESTR measures {unknown}")
    wanted = set(selection[TIME])
    cells: dict[tuple[str, str], tuple[Any, str | None]] = {}
    for first, last in date_ranges(selection[TIME]):
        params = {"fromDate": first.isoformat(), "toDate": last.isoformat()}
        rows = await _get(session, url, params, empty=(204,))
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ValueError(f"unexpected SWESTR response {str(rows)[:200]!r}")
        for row in rows:
            day = str(row.get("date"))
            if day not in wanted:
                continue
            status = _swestr_status(row)
            for measure in selection[MEASURE]:
                if row.get(measure) is not None:
                    cells[(measure, day)] = (row[measure], status)
    return cells


async def _fetch_cells(session, api, selection, data_url, metadata_url, native):
    try:
        if api == "swea":
            return await _swea(session, selection, data_url, metadata_url, native)
        if api == "mpd":
            return await _mpd(session, selection, data_url, metadata_url, native)
        return await _swestr(session, selection, data_url, metadata_url, native)
    except aiohttp.ClientResponseError as error:
        if error.status == 404 and api != "mpd":
            raise LookupError(f"Riksbank resource not found: {error}") from error
        raise


async def retrieve(
    session: aiohttp.ClientSession,
    *,
    provider_code: str,
    dataset_code: str,
    language: str,
    selection: Mapping[str, Sequence[str]],
    data_url: str | None,
    metadata_url: str | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the observation fragment for ``selection`` of one harvested dataset.

    - SWEA group (``Series`` × ``Time``): one ``Observations/{seriesId}/{from}/{to}``
      request per series and date range of at most :data:`MAX_SPAN_DAYS` days.
    - Forecasts (``Series`` × ``PolicyRound`` × ``Time``): one
      ``/forecasts?series=&policy_round_name=`` request per series and round.
    - SWESTR (``Measure`` × ``Time``): one ``/swestr/v1/SWESTR?fromDate=&toDate=``
      request per date range; ``status`` flags republished and alternatively
      calculated fixings.

    Values are numbers. Days and periods without an observation are left out
    (sparse). The caller must pace requests (the Riksbank allows about one request
    every 15 s anonymously); nothing is retried here.

    Raises:
        ValueError: The selection lacks a dimension, names an unknown dimension or
            measure, has a non-date ``Time`` code, or the Riksbank rejects it.
        LookupError: The group, series or interest rate id no longer exists.
    """
    native = metadata.get("/extension/riksbank") or {}
    api = str(native.get("api") or api_of(dataset_code) or "")
    if api not in DIMENSIONS:
        raise ValueError(f"not a Riksbank dataset code: {dataset_code!r}")
    _check_selection(api, selection, metadata)
    _dates(selection[TIME])
    cells = await _fetch_cells(session, api, selection, data_url, metadata_url, native)

    ids = list(selection)
    order = list(DIMENSIONS[api])
    positions = {
        name: {code: i for i, code in enumerate(selection[name])} for name in ids
    }
    sizes = [len(selection[name]) for name in ids]
    value: dict[str, Any] = {}
    status: dict[str, str] = {}
    for key, (cell_value, cell_status) in cells.items():
        by_name = dict(zip(order, key, strict=True))
        index = 0
        for name, size in zip(ids, sizes, strict=True):
            index = index * size + positions[name][by_name[name]]
        value[str(index)] = cell_value
        if cell_status is not None:
            status[str(index)] = cell_status

    total = 1
    for size in sizes:
        total *= size
    fragment: dict[str, Any] = {
        "id": ids,
        "size": sizes,
        "dimension": {
            name: {"category": {"index": list(selection[name])}} for name in ids
        },
        "value": (
            [value[str(i)] for i in range(total)]
            if value and len(value) == total
            else dict(sorted(value.items(), key=lambda item: int(item[0])))
        ),
    }
    if status:
        fragment["status"] = dict(sorted(status.items(), key=lambda item: int(item[0])))
    return fragment

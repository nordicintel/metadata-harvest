"""Live retrieval from Socialstyrelsen's analysverktyg (NordicIntel retrieval contract).

The analysverktyg is a public Apache Superset instance. Observations come from a
custom ``POST /api/v1/chart/data`` query on the indicator area's ``Trender`` table:
one distinct row per cell, with the value and Socialstyrelsen's comment. Imports
only ``aiohttp`` and the standard library; the service calls :func:`retrieve`
through the ``nordicintel.retrieval`` entry point ``socialstyrelsen_analys``.
"""

import asyncio
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

PROVIDER_CODE = "socialstyrelsen_analys"
SITE = "https://dataanalys.socialstyrelsen.se"
API_ROOT = f"{SITE}/api/v1"
DATA_URL = f"{API_ROOT}/chart/data"
ACCEPT_JSON = {"Accept": "application/json"}

REGION, SEX, AGE, TIME, MEASURE = "Region", "Kon", "Alder", "Tid", "ContentsCode"
"""Dimension ids of every harvested indicator, in document order."""

REQUIRED_METADATA = ("/id", "/extension/socialstyrelsen_analys")
"""The dataset's dimension ids, and ``extension.socialstyrelsen_analys`` with
``values_dataset_id`` (the Superset dataset to query) and ``columns`` (each
dimension's source column, the value and the comment column)."""

MAX_ROWS = 10_000
"""Most cells requested at once; an adapter bound on response size, not a documented
Superset limit (the server's own row cap is untested)."""
CONCURRENCY = 4
"""Most requests in flight for one split selection."""

_MISSING = re.compile(r"Columns missing in dataset: \[([^\]]*)\]")
_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")

logger = logging.getLogger("dataanalys_logger")


def query_body(
    datasource_id: int,
    columns: Sequence[str],
    filters: Sequence[Mapping[str, Any]],
    row_limit: int,
) -> dict[str, Any]:
    """A ``/chart/data`` body selecting distinct ``columns`` rows (no metrics).

    With ``metrics: []`` Superset groups by every requested column, so each row is a
    distinct combination.
    """
    return {
        "datasource": {"id": datasource_id, "type": "table"},
        "force": False,
        "result_format": "json",
        "result_type": "full",
        "queries": [
            {
                "columns": list(columns),
                "metrics": [],
                "filters": [dict(item) for item in filters],
                "row_limit": row_limit,
                "orderby": [],
                "extras": {"where": "", "having": ""},
            }
        ],
    }


def missing_columns(text: str | None) -> list[str] | None:
    """Column names of Superset's ``Columns missing in dataset: [...]`` error, or None
    when ``text`` is another answer.

    ``text`` is the response body: JSON ``{"message": ...}`` whose non-ASCII
    characters are escaped (``\\u00c5lder``), or the message itself.
    """
    try:
        decoded = json.loads(text or "")
    except ValueError:
        decoded = None
    if isinstance(decoded, Mapping) and isinstance(decoded.get("message"), str):
        text = decoded["message"]
    match = _MISSING.search(text or "")
    if match is None:
        return None
    return [single or double for single, double in _QUOTED.findall(match.group(1))]


def result_rows(body: Any) -> list[dict[str, Any]]:
    """The data rows of a ``/chart/data`` response.

    Raises:
        RuntimeError: Superset reports a query failure or an unexpected result shape.
    """
    results = body.get("result") if isinstance(body, Mapping) else None
    if not isinstance(results, list) or len(results) != 1:
        raise RuntimeError(f"Unexpected /chart/data response: {str(body)[:200]}")
    result = results[0]
    if not isinstance(result, Mapping):
        raise RuntimeError(f"Unexpected /chart/data result: {str(result)[:200]}")
    if result.get("status") == "failed" or result.get("error"):
        raise RuntimeError(f"Superset query failed: {result.get('error')}")
    rows = result.get("data")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError(f"Unexpected /chart/data rows: {str(rows)[:200]}")
    return rows


def _split_order(ids: Sequence[str]) -> list[str]:
    preferred = [name for name in (REGION, TIME) if name in ids]
    return preferred + [name for name in ids if name not in preferred]


def split_selection(
    selection: Mapping[str, Sequence[str]],
) -> list[dict[str, list[str]]]:
    """Split a selection into disjoint parts of at most :data:`MAX_ROWS` cells.

    The longest of Region and Tid is halved first; other dimensions only when both
    hold a single code.

    Raises:
        ValueError: A part of single codes still exceeds the limit (only possible
            when :data:`MAX_ROWS` is below 1).
    """
    pending = [{name: list(codes) for name, codes in selection.items()}]
    parts = []
    while pending:
        part = pending.pop()
        if math.prod(len(codes) for codes in part.values()) <= MAX_ROWS:
            parts.append(part)
            continue
        candidates = [name for name in _split_order(list(part)) if len(part[name]) > 1]
        if not candidates:
            raise ValueError(f"Selection cannot be split below {MAX_ROWS} cells")
        preferred = [name for name in candidates if name in (REGION, TIME)]
        name = max(preferred or candidates, key=lambda key: len(part[key]))
        half = len(part[name]) // 2
        for chunk in (part[name][half:], part[name][:half]):
            pending.append({**part, name: chunk})
    return parts


def _plan(columns: Mapping[str, Any], ids: Sequence[str]):
    """Source columns and output columns for the dimensions in ``ids``."""
    output: list[str] = []
    for name in ids:
        spec = columns.get(name)
        if isinstance(spec, Mapping):
            output += [str(spec["level"]), str(spec["code"])]
        elif isinstance(spec, str):
            output.append(spec)
        else:
            raise RuntimeError(f"Harvested metadata names no column for {name!r}")
    output.append(str(columns["value"]))
    if columns.get("status"):
        output.append(str(columns["status"]))
    return output


def _filters(columns: Mapping[str, Any], part: Mapping[str, Sequence[str]]):
    filters = []
    for name, codes in part.items():
        spec = columns[name]
        if isinstance(spec, Mapping):
            filters.append(
                {"col": spec["level"], "op": "IN", "val": list(spec["levels"])}
            )
            filters.append({"col": spec["code"], "op": "IN", "val": list(codes)})
        else:
            filters.append({"col": spec, "op": "IN", "val": list(codes)})
    return filters


def _code_column(spec) -> str:
    return str(spec["code"]) if isinstance(spec, Mapping) else str(spec)


async def _fetch(
    session: aiohttp.ClientSession, url: str, body: dict[str, Any]
) -> list[dict[str, Any]]:
    async with session.post(url, json=body, headers=ACCEPT_JSON) as response:
        if response.status in (400, 404):
            text = await response.text()
            logger.warning(
                "Superset answered HTTP %d for %s: %s", response.status, url, text[:200]
            )
            if response.status == 404:
                raise LookupError(f"Superset answered 404 for {url}: {text[:200]}")
            if (missing := missing_columns(text)) is not None:
                raise ValueError(f"Superset dataset lacks columns {missing}")
        response.raise_for_status()
        payload = await response.json(content_type=None)
    return result_rows(payload)


def _cells(
    rows: list[dict[str, Any]],
    columns: Mapping[str, Any],
    part: Mapping[str, Sequence[str]],
) -> dict[tuple[str, ...], tuple[Any, str | None]]:
    """Map each row's codes (in ``part`` order) to (value, status)."""
    cells_in_part = math.prod(len(codes) for codes in part.values())
    if len(rows) > cells_in_part:
        raise RuntimeError(
            f"Superset returned {len(rows)} rows for {cells_in_part} cells: "
            "a cell has more than one value"
        )
    wanted = {name: set(codes) for name, codes in part.items()}
    value_column = str(columns["value"])
    status_column = columns.get("status")
    cells: dict[tuple[str, ...], tuple[Any, str | None]] = {}
    for row in rows:
        key = tuple(row.get(_code_column(columns[name])) for name in part)
        if any(code not in wanted[name] for name, code in zip(part, key, strict=True)):
            logger.debug("Skipping row outside the selection: %s", key)
            continue
        value = row.get(value_column)
        comment = row.get(status_column) if status_column else None
        status = comment if isinstance(comment, str) and comment.strip() else None
        if isinstance(value, bool) or not (
            value is None or isinstance(value, (int, float, str))
        ):
            raise RuntimeError(f"Unexpected value {value!r} for {key}")
        if value is None and status is None:
            continue  # no data for this cell
        if key in cells:
            raise RuntimeError(f"Superset returned more than one row for {key}")
        cells[key] = (value, status)
    return cells


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
    """Return the observation fragment for ``selection`` of one harvested indicator.

    Every dimension is filtered on its source column (Region on its level and label
    columns); the distinct rows are the cells. A number in the value column is the
    value; a non-empty comment is passed verbatim in ``status`` (with the value, or
    with ``null`` when there is none); cells without a value or comment are left
    out. Selections above :data:`MAX_ROWS` cells are split.

    Raises:
        ValueError: The selection misses a dimension, or the Superset dataset no
            longer has a harvested column.
        LookupError: Superset no longer knows the dataset (HTTP 404).
        RuntimeError: Superset reports a query failure.
    """
    native = metadata.get("/extension/socialstyrelsen_analys") or {}
    columns = native.get("columns")
    datasource = native.get("values_dataset_id")
    if (
        not isinstance(columns, Mapping)
        or not isinstance(columns.get("value"), str)
        or not isinstance(datasource, int)
    ):
        raise RuntimeError(
            f"{dataset_code}: harvested metadata lacks values_dataset_id/columns"
        )
    ids = list(metadata.get("/id") or [])
    if missing := [name for name in ids if name not in selection]:
        raise ValueError(f"selection must include every dimension; missing {missing}")
    if unknown := [name for name in selection if name not in columns]:
        raise ValueError(f"unknown dimensions {unknown}")
    url = data_url or DATA_URL

    parts = split_selection(selection)
    logger.debug(
        "%s: retrieving %s cells in %d request(s)",
        dataset_code,
        " x ".join(str(len(codes)) for codes in selection.values()),
        len(parts),
    )
    if len(parts) > 1:
        logger.info("%s: selection split into %d requests", dataset_code, len(parts))
    limit = asyncio.Semaphore(CONCURRENCY)

    async def fetch(part: dict[str, list[str]]):
        cells = math.prod(len(codes) for codes in part.values())
        body = query_body(
            datasource, _plan(columns, list(part)), _filters(columns, part), cells + 1
        )
        async with limit:
            rows = await _fetch(session, url, body)
        logger.debug("POST %s %s: %d rows", url, dataset_code, len(rows))
        return part, _cells(rows, columns, part)

    results = await asyncio.gather(*(fetch(part) for part in parts))

    names = list(selection)
    positions = {
        name: {code: i for i, code in enumerate(selection[name])} for name in names
    }
    sizes = [len(selection[name]) for name in names]
    value: dict[str, Any] = {}
    status: dict[str, str] = {}
    for part, cells in results:
        for key, (cell_value, cell_status) in cells.items():
            by_name = dict(zip(part, key, strict=True))
            index = 0
            for name, size in zip(names, sizes, strict=True):
                index = index * size + positions[name][by_name[name]]
            value[str(index)] = cell_value
            if cell_status is not None:
                status[str(index)] = cell_status

    total = math.prod(sizes)
    logger.debug(
        "%s: %d of %d cells have a value, %d carry a status",
        dataset_code,
        len(value),
        total,
        len(status),
    )
    fragment: dict[str, Any] = {
        "id": names,
        "size": sizes,
        "dimension": {
            name: {"category": {"index": list(selection[name])}} for name in names
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

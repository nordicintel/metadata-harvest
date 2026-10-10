"""Live retrieval of Försäkringskassan observations (NordicIntel retrieval contract).

Imports only ``aiohttp`` and the standard library. The service calls
:func:`retrieve` through the ``nordicintel.retrieval`` entry point
``forsakringskassan``.

A table is ``GET {API_ROOT}/{report}/{Table}.json``. It answers a JSON array of
rows, one per dimension combination, with every measure side by side::

    {"dimensions": {"ar": "2025", "kon_kod": "ALL", "row_nr": "1"},
     "observations": {"antal": {"rojd": false, "value": 618.0}}}

Query parameters filter rows server-side: ``?lan_kod=01,03&ar=2026``. Several codes
are joined with commas; a repeated parameter counts only once. Unfiltered tables
can be several MB, so every request names every selected dimension.
"""

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import aiohttp

PROVIDER_CODE = "forsakringskassan"
API_ROOT = "https://www.forsakringskassan.se/api/sprstatistikrapportera/public/v1"
MEASURE_DIMENSION = "matt"
"""Dimension id holding a table's measures (``filter.matt`` in ``meta.json``)."""
ROW_COUNTER = "row_nr"
"""Row key that numbers the rows; it is not a dimension."""
SUPPRESSED = ".."
"""Status of a suppressed cell (``rojd: true``). Försäkringskassan's report texts
say: "När vi inte kan redovisa uppgifter markeras celler i tabeller med ".."."""

REQUIRED_METADATA = ("/id", "/extension/forsakringskassan")
"""The dataset's dimension ids, and its ``extension.forsakringskassan``: ``report``,
``table`` and ``measure_dimension``."""

MAX_QUERY_STRING = 1800
"""Longest encoded query string sent in one request; longer selections are split.
An adapter bound (no limit is documented), kept below common 2,048-character
URL limits."""

logger = logging.getLogger("forsakringskassan_logger")


def table_url(report: str, table: str) -> str:
    """Return the observation URL of one table."""
    return f"{API_ROOT}/{quote(report, safe='')}/{quote(table, safe='')}.json"


def query_string(filters: Mapping[str, Sequence[str]]) -> str:
    """Encode ``dimension=code,code`` pairs, keeping the separating commas literal."""
    return "&".join(
        f"{quote(name, safe='')}={','.join(quote(code, safe='') for code in codes)}"
        for name, codes in filters.items()
    )


def split_selection(
    filters: Mapping[str, Sequence[str]],
) -> list[dict[str, list[str]]]:
    """Split filters into disjoint parts whose query strings fit one request.

    A part that is too long is halved along its longest code list until it fits.

    Raises:
        ValueError: A part with one code per dimension is still too long.
    """
    pending = [{name: list(codes) for name, codes in filters.items()}]
    parts = []
    while pending:
        part = pending.pop()
        if len(query_string(part)) <= MAX_QUERY_STRING:
            parts.append(part)
            continue
        name, codes = max(part.items(), key=lambda item: len(",".join(item[1])))
        if len(codes) < 2:
            raise ValueError(
                f"Försäkringskassan query exceeds {MAX_QUERY_STRING} characters"
            )
        half = len(codes) // 2
        pending.append({**part, name: codes[half:]})
        pending.append({**part, name: codes[:half]})
    return parts


def meta_url(report: str) -> str:
    """Return the ``meta.json`` URL of one report."""
    return f"{API_ROOT}/{quote(report, safe='')}/meta.json"


async def _check_table(session: aiohttp.ClientSession, url: str, table: str) -> None:
    """Raise LookupError unless the report's ``meta.json`` still lists ``table``."""
    logger.debug("Empty table response: checking %s for table %s", url, table)
    async with session.get(url, headers={"Accept": "application/json"}) as response:
        if response.status == 404:
            logger.warning("%s answered 404: the report no longer exists", url)
            raise LookupError(f"Försäkringskassan has no report at {url}")
        response.raise_for_status()
        meta = await response.json(content_type=None)
    breakdowns = ((meta or {}).get("filter") or {}).get("uppdelning") or []
    if table not in {
        item.get("table") for item in breakdowns if isinstance(item, dict)
    }:
        logger.warning("%s no longer lists table %s", url, table)
        raise LookupError(f"Försäkringskassan no longer lists table {table!r}")


def _number(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"Försäkringskassan returned a non-numeric value {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise RuntimeError(f"Försäkringskassan returned {value!r}")
    return int(value) if isinstance(value, float) and value.is_integer() else value


async def _fetch(
    session: aiohttp.ClientSession, url: str, filters: Mapping[str, Sequence[str]]
) -> list[Any]:
    target = f"{url}?{query_string(filters)}" if filters else url
    async with session.get(target, headers={"Accept": "application/json"}) as response:
        if response.status == 404:
            raise LookupError(f"Försäkringskassan has no table at {url}")
        if response.status == 400:
            raise ValueError(f"Försäkringskassan rejected {target}")
        response.raise_for_status()
        body = await response.json(content_type=None)
    if not isinstance(body, list):
        raise RuntimeError(f"Unexpected Försäkringskassan response from {target}")
    logger.debug("GET %s: %d rows", target, len(body))
    return body


def _cells(
    rows: list[Any],
    variables: Mapping[str, Sequence[str]],
    measures: Sequence[str],
) -> dict[tuple[str, ...], tuple[float | int | None, str | None]]:
    """Map (variable codes…, measure) to (value, status) for one response."""
    wanted = {name: set(codes) for name, codes in variables.items()}
    cells: dict[tuple[str, ...], tuple[float | int | None, str | None]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("dimensions"), dict):
            raise RuntimeError(f"Malformed Försäkringskassan row {str(row)[:200]}")
        codes = {
            str(name): str(code)
            for name, code in row["dimensions"].items()
            if name != ROW_COUNTER
        }
        if set(codes) != set(wanted):
            raise RuntimeError(
                f"Försäkringskassan row dimensions {sorted(codes)} differ from the "
                f"dataset's {sorted(wanted)}"
            )
        if any(codes[name] not in allowed for name, allowed in wanted.items()):
            continue  # outside the selection
        observations = row.get("observations") or {}
        for measure in measures:
            cell = observations.get(measure)
            if not isinstance(cell, dict):
                continue  # no cell for this measure
            if cell.get("value") is None:
                if not cell.get("rojd"):
                    continue  # not reported: left out
                content: tuple[float | int | None, str | None] = (None, SUPPRESSED)
            else:
                content = (_number(cell["value"]), None)
            key = (*(codes[name] for name in variables), measure)
            if key in cells and cells[key] != content:
                raise RuntimeError(
                    f"Försäkringskassan returned conflicting cells for {key}"
                )
            cells[key] = content
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
    """Return the observation fragment for ``selection`` of one harvested table.

    Every dimension except the measure dimension becomes a server-side filter. The
    measure dimension picks measures from each row. Values are numbers; suppressed
    cells (``rojd: true``) are ``null`` with status ``".."``; cells without a value
    and without the suppression flag, and combinations without a row, are left out.

    Raises:
        ValueError: The selection lacks measures or a dimension, or is too long to
            split into requests.
        LookupError: The report or table no longer exists (HTTP 404).
    """
    native = metadata.get("/extension/forsakringskassan") or {}
    report, _, table = dataset_code.partition("/")
    report = str(native.get("report") or report)
    table = str(native.get("table") or table)
    if not report or not table:
        raise ValueError(f"dataset_code {dataset_code!r} is not 'report/Table'")
    measure_dimension = str(native.get("measure_dimension") or MEASURE_DIMENSION)
    measures = list(selection.get(measure_dimension) or [])
    if not measures:
        raise ValueError(f"selection must include measures ({measure_dimension!r})")
    if missing := [name for name in metadata.get("/id") or [] if name not in selection]:
        raise ValueError(f"selection must include every dimension; missing {missing}")
    variables = {
        name: list(codes)
        for name, codes in selection.items()
        if name != measure_dimension
    }
    if not variables:
        raise ValueError("selection must include at least one filtered dimension")
    if any(not codes for codes in variables.values()):
        raise ValueError("every selected dimension needs at least one code")
    url = data_url or table_url(report, table)
    logger.debug(
        "Retrieving %s/%s from %s: %d measures, %d filtered dimensions",
        report,
        table,
        url,
        len(measures),
        len(variables),
    )

    parts = split_selection(variables)
    if len(parts) > 1:
        logger.info("%s: selection split into %d requests", dataset_code, len(parts))
    results, empty = [], True
    for part in parts:  # sequential: one request at a time on the caller's session
        rows = await _fetch(session, url, part)
        empty = empty and not rows
        results.append(_cells(rows, part, measures))
    if empty:
        logger.info(
            "%s: every response was empty; verifying that the table exists",
            dataset_code,
        )
        # An unknown table of a known report also answers [] (not 404).
        await _check_table(session, metadata_url or meta_url(report), table)

    ids = list(selection)
    positions = {
        name: {code: i for i, code in enumerate(selection[name])} for name in ids
    }
    sizes = [len(selection[name]) for name in ids]
    order = [*variables, measure_dimension]  # key order used by _cells
    value: dict[str, float | int | None] = {}
    status: dict[str, str] = {}
    for cells in results:
        for key, (cell_value, cell_status) in cells.items():
            by_name = dict(zip(order, key, strict=True))
            index = 0
            for name, size in zip(ids, sizes, strict=True):
                index = index * size + positions[name][by_name[name]]
            value[str(index)] = cell_value
            if cell_status is not None:
                status[str(index)] = cell_status

    total = math.prod(sizes)
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
    logger.debug(
        "%s: %d of %d cells returned, %d suppressed",
        dataset_code,
        len(value),
        total,
        len(status),
    )
    if status:
        fragment["status"] = dict(sorted(status.items(), key=lambda item: int(item[0])))
    return fragment

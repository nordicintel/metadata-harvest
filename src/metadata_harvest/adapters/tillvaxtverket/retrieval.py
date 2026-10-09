"""Live retrieval of Tillväxtverket observations (NordicIntel retrieval contract).

Tillväxtverket's open data server is a Dimensional Insight DivePort (Diver/Spectre)
query service. A dataset is one ``.cbase`` file of flat rows: region spread over
``NIVA_NAMN``/``LAN_KOD``/``KOMMUN_KOD``, one column per other dimension and one
column per measure. Retrieval filters rows with the documented simple filters
``filter:<COLUMN>=<value>`` (several values of one column are OR-ed, different
columns AND-ed; https://oppnadata.tillvaxtverket.se/api.yaml), pages them with
``limit``/``offset`` and maps each row's measure columns to cells.

Imports only ``aiohttp`` and the standard library. The service calls
:func:`retrieve` through the ``nordicintel.retrieval`` entry point
``tillvaxtverket``.
"""

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

PROVIDER_CODE = "tillvaxtverket"
BASE_URL = "https://oppnadata.tillvaxtverket.se"
QUERY_ROOT = f"{BASE_URL}/api/api/query"

REGION_DIMENSION = "Region"
MEASURE_DIMENSION = "ContentsCode"
LEVEL_COLUMN = "NIVA_NAMN"
COUNTY_COLUMN = "LAN_KOD"
MUNICIPALITY_COLUMN = "KOMMUN_KOD"
REGION_COLUMNS = (LEVEL_COLUMN, COUNTY_COLUMN, MUNICIPALITY_COLUMN)
NATIONAL, COUNTY, MUNICIPALITY = "Riket", "Län", "Kommun"
SUPPRESSED = "Sekretesskyddad"
"""Tillväxtverket's text for suppressed (merged) rows and, in national rows, the
filler of the county and municipality columns."""

REQUIRED_METADATA = ("/id", "/extension/tillvaxtverket")
"""The dataset's dimension ids, and its ``extension.tillvaxtverket`` column map:
``dimension_columns`` (dimension id to source column), ``region_dimension``,
``measure_dimension`` and ``columns`` (source names and types)."""

PAGE_SIZE = 10_000
"""Rows per request: DivePort's default ``limit`` (the documented maximum is
500,000; smaller pages keep responses near 4 MB)."""
MAX_REQUESTS = 60
"""Most requests one retrieval may make (parts times pages): 600,000 rows."""
MAX_URL_LENGTH = 6_000
"""Longest request URL sent; a 6,680-character URL was accepted on 2026-10-09
(nginx), the server limit is undocumented."""

logger = logging.getLogger(__name__)


def cbase_url(area: str, name: str) -> str:
    """The query URL of one ``.cbase`` dataset, without parameters."""
    return f"{QUERY_ROOT}/{area}/data/{name}.cbase"


def region_code(level: str, county: str, municipality: str) -> str:
    """The adapter's Region category code for one row's region columns.

    ``riket`` for national rows (whose county/municipality columns only hold a
    filler such as ``Sekretesskyddad`` or ``Riket``), ``lan:<LAN_KOD>`` for county
    rows, ``kommun:<KOMMUN_KOD>`` for municipality rows and
    ``kommun:<LAN_KOD>:Sekretesskyddad`` for a county's merged suppressed
    municipalities.

    Raises:
        ValueError: The columns do not form a known region.
    """
    if level == NATIONAL:
        return "riket"
    if level == COUNTY and county and county != SUPPRESSED:
        return f"lan:{county}"
    if level == MUNICIPALITY and county and county != SUPPRESSED:
        if municipality == SUPPRESSED:
            return f"kommun:{county}:{SUPPRESSED}"
        if municipality:
            return f"kommun:{municipality}"
    raise ValueError(
        f"unknown region columns: {LEVEL_COLUMN}={level!r}, "
        f"{COUNTY_COLUMN}={county!r}, {MUNICIPALITY_COLUMN}={municipality!r}"
    )


def region_filter(code: str) -> tuple[str, str, str | None]:
    """Return ``(level, column, value)`` filtering the rows of a Region code.

    Raises:
        ValueError: The code is not an adapter Region code.
    """
    if code == "riket":
        return NATIONAL, LEVEL_COLUMN, None
    kind, _, rest = code.partition(":")
    if kind == "lan" and rest and ":" not in rest:
        return COUNTY, COUNTY_COLUMN, rest
    if kind == "kommun" and rest:
        county, _, suppressed = rest.partition(":")
        if not suppressed:
            return MUNICIPALITY, MUNICIPALITY_COLUMN, county
        if suppressed == SUPPRESSED:
            return MUNICIPALITY, MUNICIPALITY_COLUMN, SUPPRESSED
    raise ValueError(f"unknown Region code {code!r}")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cell(
    raw: Any, column_type: str | None
) -> tuple[bool, float | int | None, str | None]:
    """Return ``(present, value, status)`` for one measure value.

    Numbers stay numbers; the source declares every measure column ``integer`` but
    JSON sometimes carries such values as floats (``1.6737801E7``), so integral
    floats in an ``integer`` column become ints. Non-numeric text (such as
    ``Sekretesskyddad``) is a marker: ``None`` with the text as status. ``None`` or
    an empty string means the source has no value for the cell.
    """
    if raw is None or isinstance(raw, bool):
        return (False, None, None) if raw is None else (True, None, str(raw))
    if isinstance(raw, int):
        return True, raw, None
    if isinstance(raw, float):
        if column_type == "integer" and raw.is_integer():
            return True, int(raw), None
        return True, raw, None
    text = str(raw).strip()
    if not text:
        return False, None, None
    try:
        number = float(text)
    except ValueError:
        return True, None, text
    if not math.isfinite(number):
        return True, None, text
    if number.is_integer() and (column_type == "integer" or text.lstrip("-").isdigit()):
        return True, int(number), None
    return True, number, None


def _split_url(data_url: str) -> tuple[str, list[tuple[str, str]]]:
    """Split ``data_url`` into its base and the parameters this module keeps."""
    parts = urlsplit(data_url)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"format", "limit", "offset"}
        and not key.startswith(("filter", "column", "dimension"))
    ]
    return base, kept


class _Part:
    """One request's filters: column to the values requested (OR-ed)."""

    def __init__(self, level: str, filters: dict[str, list[str]]):
        self.level = level
        self.filters = filters

    def params(self, columns: Sequence[str]) -> list[tuple[str, str]]:
        params = [("filter:" + LEVEL_COLUMN, self.level)] if self.level else []
        for column, values in self.filters.items():
            params.extend(("filter:" + column, value) for value in values)
        params.extend(("column", column) for column in columns)
        return params


def _url_length(base: str, params: list[tuple[str, str]]) -> int:
    paging = [("format", "json"), ("limit", str(PAGE_SIZE)), ("offset", "9" * 9)]
    return len(base) + 1 + len(urlencode([*paging, *params]))


def split_parts(
    base: str, parts: list[_Part], columns: Sequence[str], fixed: list[tuple[str, str]]
) -> list[_Part]:
    """Halve each part along its longest filter list until its URL fits.

    Raises:
        ValueError: A part with single-value filters is still too long.
    """
    pending, done = list(reversed(parts)), []
    while pending:
        part = pending.pop()
        params = [*fixed, *part.params(columns)]
        if _url_length(base, params) <= MAX_URL_LENGTH:
            done.append(part)
            continue
        column, values = max(
            part.filters.items(),
            key=lambda item: (
                len(urlencode([(item[0], v) for v in item[1]]))
                if len(item[1]) > 1
                else -1
            ),
        )
        if len(values) < 2:
            raise ValueError(
                f"Tillväxtverket request exceeds {MAX_URL_LENGTH} characters "
                "and cannot be split"
            )
        half = len(values) // 2
        for chunk in (values[half:], values[:half]):
            pending.append(_Part(part.level, {**part.filters, column: chunk}))
    return done


async def _get(
    session: aiohttp.ClientSession, url: str, params: list[tuple[str, str]]
) -> dict[str, Any]:
    async with session.get(url, params=params) as response:
        if response.status == 404:
            raise LookupError(f"Tillväxtverket dataset not found: {url}")
        if response.status >= 500:
            text = await response.text()
            if "File not found" in text:
                raise LookupError(f"Tillväxtverket dataset not found: {url}: {text}")
            if "not found" in text and "Column" in text:
                raise ValueError(f"Tillväxtverket rejected the selection: {text}")
        elif response.status == 400:
            raise ValueError(
                f"Tillväxtverket rejected the selection: {await response.text()}"
            )
        response.raise_for_status()
        body = await response.json(content_type=None)
    if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
        raise RuntimeError(f"Tillväxtverket answered without rows: {url}")
    return body


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

    The Region codes select rows per region level (one request group per level);
    every other dimension filters its source column; the measure dimension selects
    the measure columns returned. Values are numbers; a non-numeric measure value
    (such as ``Sekretesskyddad``) is ``null`` with the text in ``status``; cells
    without a source row or value are left out.

    Raises:
        ValueError: The selection lacks a dimension, cannot be expressed or split
            under :data:`MAX_REQUESTS`/:data:`MAX_URL_LENGTH`, or is rejected.
        LookupError: The dataset no longer exists at Tillväxtverket.
    """
    native = metadata.get("/extension/tillvaxtverket") or {}
    dimension_columns: dict[str, str] = dict(native.get("dimension_columns") or {})
    if not dimension_columns:
        raise ValueError("metadata lacks /extension/tillvaxtverket/dimension_columns")
    region_dimension = str(native.get("region_dimension") or REGION_DIMENSION)
    measure_dimension = str(native.get("measure_dimension") or MEASURE_DIMENSION)
    types = {
        str(column.get("name")): column.get("type")
        for column in native.get("columns") or []
    }
    ids = list(metadata.get("/id") or [])
    if missing := [name for name in ids if name not in selection]:
        raise ValueError(f"selection must include every dimension; missing {missing}")
    known = {region_dimension, measure_dimension, *dimension_columns}
    if unknown := [name for name in selection if name not in known]:
        raise ValueError(f"selection names unknown dimensions {unknown}")
    measures = list(selection.get(measure_dimension) or [])
    if not measures:
        raise ValueError(f"selection must include measures ({measure_dimension!r})")
    if types and (unknown := [m for m in measures if m not in types]):
        raise ValueError(f"unknown measures {unknown}")
    if not data_url:
        area, _, name = dataset_code.partition("/")
        data_url = cbase_url(area, name)
    base, fixed = _split_url(data_url)

    plain = {
        dimension: column
        for dimension, column in dimension_columns.items()
        if dimension in selection
    }
    common = {column: list(selection[dim]) for dim, column in plain.items()}
    levels: dict[str, dict[str, list[str]]] = {}
    regions = list(selection.get(region_dimension) or [])
    if region_dimension in selection or region_dimension in ids:
        if not regions:
            raise ValueError(f"selection must include {region_dimension!r} codes")
        counties_of_suppressed = set()
        for code in regions:
            level, column, value = region_filter(code)
            filters = levels.setdefault(level, {})
            if value is not None:
                values = filters.setdefault(column, [])
                if value not in values:
                    values.append(value)
            if value == SUPPRESSED:
                counties_of_suppressed.add(code.split(":")[1])
        municipal = levels.get(MUNICIPALITY, {}).get(MUNICIPALITY_COLUMN, [])
        if municipal == [SUPPRESSED]:  # only merged rows: narrow to their counties
            levels[MUNICIPALITY][COUNTY_COLUMN] = sorted(counties_of_suppressed)
    else:
        levels = {"": {}}
    columns = [
        *(REGION_COLUMNS if region_dimension in selection else ()),
        *plain.values(),
        *measures,
    ]
    parts = [
        _Part(level, {**filters, **common}) for level, filters in sorted(levels.items())
    ]
    rows_bound = math.prod(
        len(codes) for name, codes in selection.items() if name != measure_dimension
    )
    if math.ceil(rows_bound / PAGE_SIZE) > MAX_REQUESTS:
        raise ValueError(
            f"selection spans up to {rows_bound} rows; more than {MAX_REQUESTS} "
            f"requests of {PAGE_SIZE} rows"
        )
    fixed = [("format", "json"), *fixed]
    parts = split_parts(base, parts, columns, fixed)
    if len(parts) > MAX_REQUESTS:
        raise ValueError(f"selection needs {len(parts)} requests (cap {MAX_REQUESTS})")
    if len(parts) > 1:
        logger.info("%s: selection split into %d requests", dataset_code, len(parts))

    ids = list(selection)
    positions = {
        name: {code: i for i, code in enumerate(selection[name])} for name in ids
    }
    sizes = [len(selection[name]) for name in ids]
    cells: dict[int, tuple[float | int | None, str | None]] = {}
    requests = 0
    planned = len(parts)
    for part in parts:
        params = [*fixed, *part.params(columns)]
        offset = 0
        while True:
            page = [*params, ("limit", str(PAGE_SIZE)), ("offset", str(offset))]
            body = await _get(session, base, page)
            requests += 1
            if offset == 0:
                count = int(body.get("rowCount") or 0)
                planned += max(0, math.ceil(count / PAGE_SIZE) - 1)
                if planned > MAX_REQUESTS:
                    raise ValueError(
                        f"selection spans {count} rows in one part; more than "
                        f"{MAX_REQUESTS} requests of {PAGE_SIZE} rows"
                    )
            rows = body["rows"]
            for row in rows:
                coordinates = {}
                if region_dimension in selection:
                    try:
                        coordinates[region_dimension] = region_code(
                            _text(row.get(LEVEL_COLUMN)),
                            _text(row.get(COUNTY_COLUMN)),
                            _text(row.get(MUNICIPALITY_COLUMN)),
                        )
                    except ValueError:
                        logger.debug("Skipping row with unknown region: %s", row)
                        continue
                for dimension, column in plain.items():
                    coordinates[dimension] = _text(row.get(column))
                if any(
                    coordinates[name] not in positions[name] for name in coordinates
                ):
                    continue  # outside the selection (filters can over-fetch)
                for measure in measures:
                    present, value, status = _cell(row.get(measure), types.get(measure))
                    if not present:
                        continue
                    index = 0
                    for name, size in zip(ids, sizes, strict=True):
                        code = (
                            measure if name == measure_dimension else coordinates[name]
                        )
                        index = index * size + positions[name][code]
                    if index in cells and cells[index] != (value, status):
                        raise RuntimeError(
                            f"Tillväxtverket returned conflicting rows for "
                            f"{coordinates} {measure}"
                        )
                    cells[index] = (value, status)
            offset += len(rows)
            if not rows or offset >= int(body.get("rowCount") or 0):
                break
    logger.debug("%s: %d requests, %d cells", dataset_code, requests, len(cells))

    total = math.prod(sizes)
    ordered = sorted(cells)
    value_map = {str(i): cells[i][0] for i in ordered}
    status = {str(i): cells[i][1] for i in ordered if cells[i][1] is not None}
    fragment: dict[str, Any] = {
        "id": ids,
        "size": sizes,
        "dimension": {
            name: {"category": {"index": list(selection[name])}} for name in ids
        },
        "value": (
            [value_map[str(i)] for i in range(total)]
            if value_map and len(value_map) == total
            else value_map
        ),
    }
    if status:
        fragment["status"] = status
    return fragment

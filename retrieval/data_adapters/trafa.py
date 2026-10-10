"""Live retrieval of Trafikanalys observations (NordicIntel retrieval contract).

Imports only ``aiohttp`` and the standard library. The service calls
:func:`retrieve` through the ``nordicintel.retrieval`` entry point ``trafa``.
"""

import asyncio
import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import aiohttp

PROVIDER_CODE = "trafa"
API_ROOT = "https://api.trafa.se/api"
DATA_URL = f"{API_ROOT}/data"
ACCEPT_JSON = {"Accept": "application/json"}
"""Headers for every Trafikanalys request: the API answers XML unless JSON is asked."""
MEASURE_DIMENSION = "measure"
"""Default dimension id holding a dataset's Trafikanalys measures (``M`` items)."""

REQUIRED_METADATA = ("/id", "/extension/trafa")
"""The dataset's dimension ids, and its ``extension.trafa``: ``product`` (query name)
and ``measure_dimension``."""

MAX_QUERY_STRING = 2000
"""Longest encoded query string sent. Trafikanalys answers HTTP 404 above 2048
characters (2038 accepted, 2059 rejected on 2026-09-25)."""
MAX_ROWS = 20_000
"""Largest number of rows (variable-category combinations) requested at once; an
adapter bound on response size (about 1 kB per row), not a documented limit."""
CONCURRENCY = 4
"""Most requests in flight for one split selection."""

_NOT_FOUND = "hittades inte i strukturen"
"""Trafikanalys' error text for unknown products and items (``Errors``)."""
_NUMBER = re.compile(r"-?\d+(?:[,.]\d+)?")
"""A measure ``Value``: decimals use a comma (``"59,2072702533887"``), no grouping."""

logger = logging.getLogger(__name__)


def api_language(language: str) -> str:
    """Return the Trafikanalys ``lang`` value (``SV``/``EN``) for ``sv``/``en``."""
    return language.upper()


def _query(
    product: str, measures: Sequence[str], variables: Mapping[str, Sequence[str]]
):
    parts = [product, *measures]
    parts.extend(f"{name}:{','.join(codes)}" for name, codes in variables.items())
    return "|".join(parts)


def _query_string_length(query: str, language: str) -> int:
    # urlencode percent-encodes "|", ":" and ",": the longest form a client sends.
    return len(urlencode({"query": query, "lang": api_language(language)}))


def split_selection(
    product: str,
    measures: Sequence[str],
    variables: Mapping[str, Sequence[str]],
    language: str,
) -> list[tuple[list[str], dict[str, list[str]]]]:
    """Split a selection into parts that each fit one Trafikanalys request.

    A part is too large when its encoded query string exceeds
    :data:`MAX_QUERY_STRING` or it spans more than :data:`MAX_ROWS` rows. Such a
    part is halved along its longest code list, measures included, until every part
    fits. Parts are disjoint and together cover the selection.

    Raises:
        ValueError: A single-code part is still too long.
    """
    pending = [
        (list(measures), {name: list(codes) for name, codes in variables.items()})
    ]
    parts = []
    while pending:
        part_measures, part_variables = pending.pop()
        query = _query(product, part_measures, part_variables)
        rows = math.prod(len(codes) for codes in part_variables.values())
        if (
            _query_string_length(query, language) <= MAX_QUERY_STRING
            and rows <= MAX_ROWS
        ):
            parts.append((part_measures, part_variables))
            continue
        lists = {name: codes for name, codes in part_variables.items()}
        lists[None] = part_measures
        name, codes = max(
            lists.items(),
            key=lambda item: len(",".join(item[1])) if len(item[1]) > 1 else -1,
        )
        if len(codes) < 2:
            raise ValueError(
                f"Trafikanalys query exceeds {MAX_QUERY_STRING} characters: {query[:200]}…"
            )
        half = len(codes) // 2
        for chunk in (codes[half:], codes[:half]):
            if name is None:
                pending.append((chunk, part_variables))
            else:
                pending.append((part_measures, {**part_variables, name: chunk}))
    parts.reverse()
    return parts


def _check_errors(body: Mapping[str, Any], query: str) -> None:
    errors = [str(error) for error in body.get("Errors") or []]
    if not errors:
        return
    message = f"Trafikanalys rejected {query!r}: {'; '.join(errors)}"
    if any(_NOT_FOUND in error for error in errors):
        raise LookupError(message)
    raise ValueError(message)


async def _fetch(
    session: aiohttp.ClientSession, url: str, query: str, language: str
) -> dict[str, Any]:
    params = {"query": query, "lang": api_language(language)}
    async with session.get(url, params=params, headers=ACCEPT_JSON) as response:
        if response.status == 500:
            text = await response.text()
            # Unknown category codes make Trafikanalys' query parser throw.
            if "FromQueryStringToQuery" in text:
                raise ValueError(
                    f"Trafikanalys could not parse {query!r}; a selected code is "
                    "probably no longer available"
                )
        response.raise_for_status()
        body = await response.json(content_type=None)
    logger.debug("GET %s %s: %d rows", url, query, len(body.get("Rows") or []))
    _check_errors(body, query)
    return body


def _parse_value(text: str) -> float | int | None:
    """Return the number in a measure value, or None for markers such as ``-``."""
    if not _NUMBER.fullmatch(text):
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return float(text.replace(",", "."))


def _cells(
    body: Mapping[str, Any],
    query: str,
    measures: Sequence[str],
    variables: Mapping[str, Sequence[str]],
) -> dict[tuple[str, ...], tuple[float | int | None, str | None]]:
    """Map (variable codes…, measure) to (value, status) for one response."""
    columns = {
        str(column.get("Name")) for column in body.get("Header", {}).get("Column", [])
    }
    if missing := [name for name in (*variables, *measures) if name not in columns]:
        raise LookupError(f"Trafikanalys response to {query!r} lacks {missing}")
    wanted = {name: set(codes) for name, codes in variables.items()}
    notes = {str(key): str(text) for key, text in (body.get("Notes") or {}).items()}
    cells: dict[tuple[str, ...], tuple[float | int | None, str | None]] = {}
    for row in body.get("Rows") or []:
        codes: dict[str, str] = {}
        measure_cells = []
        for cell in row.get("Cell") or []:
            column = str(cell.get("Column"))
            if cell.get("IsMeasure"):
                measure_cells.append(cell)
            elif column in wanted:
                codes[column] = str(cell.get("Name"))
        if any(codes.get(name) not in allowed for name, allowed in wanted.items()):
            logger.debug("Skipping row outside the selection: %s", codes)
            continue
        for cell in measure_cells:
            measure = str(cell.get("Column"))
            if measure not in measures:
                continue
            text = str(
                cell.get("Value") if cell.get("Value") is not None else ""
            ).strip()
            flags = [notes.get(str(i), str(i)) for i in cell.get("NoteIds") or []]
            if not text and not flags:
                continue  # no data for this cell
            value = _parse_value(text)
            if value is None and text:
                flags.insert(0, text)
            key = (*(codes[name] for name in variables), measure)
            status = "; ".join(flags) or None
            if key in cells and cells[key] != (value, status):
                raise RuntimeError(f"Trafikanalys returned conflicting cells for {key}")
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
    """Return the observation fragment for ``selection`` of one harvested dataset.

    The measure dimension's codes are queried as Trafikanalys measures, every other
    dimension as ``name:code,…``. Large selections are split
    (see :func:`split_selection`). Values are numbers; marker cells (such as ``-``)
    are ``null`` with the marker in ``status``; cell notes (such as "Reviderad
    uppgift") are added to ``status``, joined with ``"; "``; cells without data are
    left out.

    Raises:
        ValueError: The selection lacks measures or dimensions, is too large to
            split, or Trafikanalys rejects it.
        LookupError: Trafikanalys no longer knows the product or one of its items.
    """
    trafa = metadata.get("/extension/trafa") or {}
    product = str(trafa.get("product") or dataset_code.split("|", 1)[0].lower())
    measure_dimension = str(trafa.get("measure_dimension") or MEASURE_DIMENSION)
    measures = list(selection.get(measure_dimension) or [])
    if not measures:
        raise ValueError(f"selection must include measures ({measure_dimension!r})")
    variables = {
        name: list(codes)
        for name, codes in selection.items()
        if name != measure_dimension
    }
    # Only Trafikanalys' listed API combinations answer reliably, so every
    # variable is queried; elimination uses the harvested "t1" elimination_value.
    if missing := [name for name in metadata.get("/id") or [] if name not in selection]:
        raise ValueError(f"selection must include every dimension; missing {missing}")
    url = data_url or DATA_URL

    parts = split_selection(product, measures, variables, language)
    if len(parts) > 1:
        logger.info("%s: selection split into %d requests", dataset_code, len(parts))
    limit = asyncio.Semaphore(CONCURRENCY)

    async def fetch(part_measures: list[str], part_variables: dict[str, list[str]]):
        query = _query(product, part_measures, part_variables)
        async with limit:
            body = await _fetch(session, url, query, language)
        return _cells(body, query, part_measures, part_variables)

    results = await asyncio.gather(*(fetch(*part) for part in parts))

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
    if status:
        fragment["status"] = dict(sorted(status.items(), key=lambda item: int(item[0])))
    return fragment

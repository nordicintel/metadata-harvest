"""Live retrieval of EntryScape RowStore observations.

Imports only ``aiohttp`` and the standard library. A dataset is one or more
RowStore tables (``{base}/rowstore/dataset/{uuid}``) answering
``?column=value&_limit=500&_offset=N``; a filter starting with ``^`` is a
PostgreSQL regular expression, anything else an exact match.

Rows matching the selection are read from the tables the ``part`` selection
covers (every table when ``part`` is omitted), then summed per combination of
the selected dimensions: dropped identifier columns, unlisted wide columns and
omitted dimensions all collapse into their cell. ``status`` reports ``rows=N``
for a cell built from more than one row and the text of a non-numeric value.
"""

import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

logger = logging.getLogger("entryscape_logger")

REQUIRED_METADATA = ("/id", "/dimension", "/extension/entryscape")
PAGE_SIZE = 500
MAX_REQUESTS = 50
"""A selection needing more pages is refused rather than paid for."""
MEASURE_DIMENSION = "measure"
PART_DIMENSION = "part"
ROW_COUNT = "rows"
BLANK = "(tom)"
"""Category code standing for a blank cell value."""
_NUMBER = re.compile(r"^[-−]?(?:\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)?$")
_REGEX_SPECIAL = set("\\^$.|?*+()[]{}")


def regex_escape(text: str) -> str:
    return "".join("\\" + char if char in _REGEX_SPECIAL else char for char in text)


def clean_name(column: str) -> str:
    return column.replace("﻿", "").strip()


def filter_value(values: Sequence[str]) -> str:
    """The RowStore filter selecting exactly ``values`` of one column."""
    if len(values) == 1 and values[0] != BLANK and not values[0].startswith("^"):
        return values[0]
    parts = ["\\s*" if value == BLANK else regex_escape(value) for value in values]
    return "^(?:" + "|".join(parts) + ")$"


def number(text: object) -> float | int | None:
    text = str(text).strip().replace("−", "-")
    if not _NUMBER.match(text):
        return None
    value = float(re.sub(r"[  ]", "", text).replace(",", "."))
    return (
        int(value)
        if value.is_integer() and "," not in text and "." not in text
        else value
    )


def _code(value: object) -> str:
    text = "" if value is None else str(value)
    return BLANK if not text.strip() else text


async def _page(session: aiohttp.ClientSession, url: str, params: dict) -> dict:
    async with session.get(
        url, params=params, headers={"Accept": "application/json"}
    ) as response:
        if response.status == 404:
            logger.warning("RowStore has no table at %s (HTTP 404)", url)
            raise LookupError(f"RowStore has no table at {url}")
        response.raise_for_status()
        try:
            body = await response.json(content_type=None)
        except ValueError as error:
            raise RuntimeError(f"RowStore answered {url} with non-JSON") from error
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise RuntimeError(f"RowStore answered {url} with {str(body)[:200]!r}")
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
    """Return the observation fragment for ``selection`` of one dataset.

    Raises:
        ValueError: The selection names an unknown part or measure, or needs
            more than ``MAX_REQUESTS`` pages.
        LookupError: A table of the dataset no longer exists.
        RuntimeError: RowStore answered something other than a row page.
    """
    dimensions = metadata.get("/dimension") or {}
    native = metadata.get("/extension/entryscape") or {}
    tables = list(native.get("tables") or ([data_url] if data_url else []))
    logger.info(
        "Retrieving %s/%s [%s]: %d table(s), selection dimensions %s",
        provider_code,
        dataset_code,
        language,
        len(tables),
        {name: len(values) for name, values in selection.items()},
    )
    if not tables:
        logger.warning("%s/%s lists no RowStore tables", provider_code, dataset_code)
        raise ValueError("The dataset lists no RowStore tables")
    if MEASURE_DIMENSION not in selection:
        logger.warning(
            "Selection for %s/%s has no measure", provider_code, dataset_code
        )
        raise ValueError("The selection must include the measure dimension")

    def spec(name):
        return dimensions.get(name, {}).get("extension", {}).get("entryscape", {})

    part_of: dict[str, str] = {}
    if PART_DIMENSION in dimensions:
        mapping = spec(PART_DIMENSION).get("tables") or {}
        part_of = {url: label for label, url in mapping.items()}
        if PART_DIMENSION in selection:
            unknown = [p for p in selection[PART_DIMENSION] if p not in mapping]
            if unknown:
                logger.warning("Unknown parts %s requested", unknown)
                raise ValueError(f"Unknown parts {unknown}")
            tables = [mapping[p] for p in selection[PART_DIMENSION]]
            logger.debug(
                "Part selection narrows %d tables to %d", len(part_of), len(tables)
            )
    filters = {}
    for name, values in selection.items():
        if name in (PART_DIMENSION, MEASURE_DIMENSION):
            continue
        column = spec(name).get("column")
        if not column:
            raise ValueError(f"Dimension {name!r} has no RowStore column")
        filters[column] = filter_value(list(values))
    logger.debug("RowStore filters on columns %s", sorted(filters))
    measures = list(selection[MEASURE_DIMENSION])
    row_count = (
        measures == [ROW_COUNT]
        and spec(MEASURE_DIMENSION).get("measure_kind") == "row_count"
    )

    ids = list(selection)
    axes = {name: list(selection[name]) for name in ids}
    positions = {name: {code: i for i, code in enumerate(axes[name])} for name in ids}
    columns = {name: spec(name).get("column") for name in ids}
    sums: dict[tuple, list] = {}
    counts: dict[tuple, int] = {}
    markers: dict[tuple, set] = {}
    requests = 0
    for url in tables:
        offset = 0
        while True:
            if requests >= MAX_REQUESTS:
                logger.warning(
                    "Selection for %s/%s needs more than %d requests; refusing",
                    provider_code,
                    dataset_code,
                    MAX_REQUESTS,
                )
                raise ValueError(
                    f"The selection needs more than {MAX_REQUESTS} requests"
                )
            page = await _page(
                session, url, {**filters, "_limit": PAGE_SIZE, "_offset": offset}
            )
            requests += 1
            rows = page["results"]
            logger.debug(
                "Table %s offset %d: %d rows of %s",
                url,
                offset,
                len(rows),
                page.get("resultCount"),
            )
            for raw in rows:
                row = {clean_name(k): v for k, v in raw.items()}
                key = []
                for name in ids:
                    if name == MEASURE_DIMENSION:
                        continue
                    code = (
                        part_of.get(url, "")
                        if name == PART_DIMENSION
                        else _code(row.get(columns[name]))
                    )
                    position = positions[name].get(code)
                    if position is None:
                        key = None  # a row outside the selection (regex over-match)
                        break
                    key.append(position)
                if key is None:
                    continue
                key = tuple(key)
                cell = sums.setdefault(key, [None] * len(measures))
                counts[key] = counts.get(key, 0) + 1
                for index, measure in enumerate(measures):
                    if row_count:
                        cell[index] = (cell[index] or 0) + 1
                        continue
                    text = row.get(measure, "")
                    value = number(text)
                    if value is None:
                        if str(text).strip():
                            markers.setdefault(key, set()).add(str(text).strip())
                        continue
                    cell[index] = (cell[index] or 0) + value
            offset += len(rows)
            total = int(page.get("resultCount") or 0)
            if len(rows) < PAGE_SIZE or offset >= total:
                break

    logger.info(
        "Retrieved %s/%s: %d requests, %d rows folded into %d cells",
        provider_code,
        dataset_code,
        requests,
        sum(counts.values()),
        len(sums),
    )
    sizes = [len(axes[name]) for name in ids]
    measure_axis = ids.index(MEASURE_DIMENSION)
    value: dict[str, Any] = {}
    status: dict[str, str] = {}
    for key, cell in sums.items():
        flags = []
        if counts[key] > 1:
            flags.append(f"rows={counts[key]}")
        flags.extend(sorted(markers.get(key, ())))
        for index, measure in enumerate(measures):
            coordinates = list(key)
            coordinates.insert(measure_axis, positions[MEASURE_DIMENSION][measure])
            flat = 0
            for position, size in zip(coordinates, sizes, strict=True):
                flat = flat * size + position
            value[str(flat)] = cell[index]
            if flags:
                status[str(flat)] = "; ".join(flags)
    total = math.prod(sizes)
    fragment: dict[str, Any] = {
        "id": ids,
        "size": sizes,
        "dimension": {name: {"category": {"index": axes[name]}} for name in ids},
        "value": [value[str(i)] for i in range(total)]
        if len(value) == total
        else value,
    }
    if status:
        fragment["status"] = status
    return fragment

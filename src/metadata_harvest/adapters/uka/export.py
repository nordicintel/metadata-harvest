"""UKÄ per-period CSV exports: layout, category codes and observation values.

Standard library only; shared by live retrieval and harvesting so that both derive
dimension and category codes from an export in exactly the same way.

An export (``/api/totals/{id}?export=all&export_type=csv&year=<period>``) is a
``;``-separated UTF-8 file, with or without BOM and quoting, holding one period's
full cube: the first column is the period, the last the measure, and the columns in
between are breakdowns. A breakdown whose header ends in ``[n]`` is level ``n`` of a
hierarchy; consecutive levels ``[0]``, ``[1]``, … form one dimension. A blank cell or
``Total`` marks the aggregate over a breakdown (or over a hierarchy level).
"""

import csv
import io
import re
from collections.abc import Iterator
from dataclasses import dataclass

PROVIDER_CODE = "uka"
API_ROOT = "https://statistik-api.uka.se/api"
EXPORT_PARAMS = {"export": "all", "export_type": "csv"}
"""Query parameters that turn ``/api/totals/{id}`` into an export link request."""

NO_PERIOD_ERROR = "No dataset for indicator"
"""Start of UKÄ's error text (HTTP 200) for a period an indicator has no export for:
``{"error": "No dataset for indicator ID: 97 and year XX1999"}``."""

TOTAL = "Total"
"""Category code (and label) of the aggregate category of every breakdown."""
PATH_SEPARATOR = " > "
"""Joins hierarchy levels into a category code, e.g. ``Humaniora och teologi > …``."""

_LEVEL = re.compile(r"^(?P<name>.*\S)\s*\[(?P<level>\d+)\]$")
_NUMBER = re.compile(r"^-?\d+(?:,\d+)?$")
"""A provided value: optional minus, digits, optional decimal comma (``30,5``)."""


@dataclass(frozen=True)
class Dimension:
    """A breakdown dimension and the export columns (one per level) it spans."""

    code: str
    columns: tuple[str, ...]

    @property
    def hierarchical(self) -> bool:
        return any(_LEVEL.match(column) for column in self.columns)


@dataclass(frozen=True)
class Layout:
    """The column structure of one export file."""

    period_column: str
    measure_column: str
    dimensions: tuple[Dimension, ...]

    @property
    def dimension_codes(self) -> tuple[str, ...]:
        return tuple(dimension.code for dimension in self.dimensions)


def parse_layout(header: list[str]) -> Layout:
    """Return the layout of an export from its header row.

    Raises:
        ValueError: The header has fewer than two columns, a hierarchy level out of
            sequence, or two breakdowns with the same dimension code.
    """
    columns = [column.strip() for column in header]
    if len(columns) < 2 or not all(columns):
        raise ValueError(f"unexpected UKÄ export header {header!r}")
    dimensions: list[Dimension] = []
    group: list[str] = []
    for column in columns[1:-1]:
        match = _LEVEL.match(column)
        level = int(match["level"]) if match else None
        if group and level is not None and level == len(group):
            group.append(column)
            continue
        if group:
            dimensions.append(_dimension(group))
            group = []
        if level is None:
            dimensions.append(Dimension(column, (column,)))
        elif level == 0:
            group = [column]
        else:
            raise ValueError(f"hierarchy level out of sequence in {header!r}")
    if group:
        dimensions.append(_dimension(group))
    codes = [columns[0], *(dimension.code for dimension in dimensions)]
    if len(set(codes)) != len(codes):
        raise ValueError(f"duplicate dimension codes {codes} in {header!r}")
    return Layout(columns[0], columns[-1], tuple(dimensions))


def _dimension(columns: list[str]) -> Dimension:
    match = _LEVEL.match(columns[0])
    assert match is not None
    return Dimension(match["name"], tuple(columns))


def _is_total(cell: str) -> bool:
    return cell == "" or cell == TOTAL


def category_path(cells: list[str]) -> tuple[str, ...]:
    """Return the hierarchy path a row's level cells identify (empty for the total).

    Levels below the deepest non-total cell must be totals as well.

    Raises:
        ValueError: A deeper level is set below a total level.
    """
    path: list[str] = []
    for position, cell in enumerate(cells):
        if _is_total(cell):
            if any(not _is_total(deeper) for deeper in cells[position + 1 :]):
                raise ValueError(f"hierarchy cells {cells!r} skip a level")
            break
        path.append(cell)
    return tuple(path)


def category_code(path: tuple[str, ...]) -> str:
    """Return the category code of a hierarchy path (or flat cell)."""
    return PATH_SEPARATOR.join(path) if path else TOTAL


def parse_value(cell: str) -> tuple[int | float | None, str | None] | None:
    """Return ``(value, marker)`` for a measure cell, or None for a blank cell.

    Numbers become ``int`` or ``float`` (decimal comma); any other text is a marker,
    returned with value None.
    """
    if cell == "":
        return None
    if _NUMBER.match(cell):
        if "," in cell:
            return float(cell.replace(",", ".")), None
        return int(cell), None
    return None, cell


@dataclass(frozen=True)
class Row:
    """One export row: period, category code per dimension, value and marker."""

    period: str
    categories: tuple[str, ...]
    """Category codes in :attr:`Layout.dimensions` order."""
    paths: tuple[tuple[str, ...], ...]
    """Hierarchy path per dimension (a flat category is a one-element path)."""
    value: int | float | None
    marker: str | None


def read_export(text: str) -> tuple[Layout, Iterator[Row]]:
    """Parse export text into its layout and rows with data.

    Rows whose measure cell is blank carry no data and are skipped. Every cell is
    stripped of surrounding whitespace (UKÄ writes some periods as ``" 2024/25"``).

    Raises:
        ValueError: The text is empty, the header is malformed, or a row has the
            wrong number of cells (raised while iterating).
    """
    reader = csv.reader(io.StringIO(text.removeprefix("﻿")), delimiter=";")
    header = next(reader, None)
    if header is None:
        raise ValueError("empty UKÄ export")
    layout = parse_layout(header)
    return layout, _rows(reader, layout, len(header))


def _rows(reader: Iterator[list[str]], layout: Layout, width: int) -> Iterator[Row]:
    for number, raw in enumerate(reader, start=2):
        if not raw:
            continue
        if len(raw) != width:
            raise ValueError(f"export line {number} has {len(raw)} cells, not {width}")
        cells = [cell.strip() for cell in raw]
        parsed = parse_value(cells[-1])
        if parsed is None:
            continue
        position = 1
        paths = []
        for dimension in layout.dimensions:
            span = cells[position : position + len(dimension.columns)]
            position += len(dimension.columns)
            paths.append(category_path(span))
        yield Row(
            period=cells[0],
            categories=tuple(category_code(path) for path in paths),
            paths=tuple(paths),
            value=parsed[0],
            marker=parsed[1],
        )

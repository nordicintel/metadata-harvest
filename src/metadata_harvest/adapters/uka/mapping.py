"""Build NordicIntel Dataset documents from an indicator's export files."""

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

from metadata_harvest.adapters.uka.export import (
    EXPORT_PARAMS,
    PROVIDER_CODE,
    TOTAL,
    Layout,
    category_code,
    read_export,
)
from metadata_harvest.adapters.uka.source import (
    Entry,
    Indicator,
    documentation_url,
    is_discontinued,
    plain_text,
    totals_url,
)

LANGUAGE = "sv"
"""UKÄ publishes the indicators in Swedish only."""
MAPPING_VERSION = 1
"""Increment when a mapping change must rebuild every accepted document."""
TIME_UNITS = {"Termin": "semiannual", "Läsår": "annual", "År": "annual"}
"""Period column header to ``time_unit``: terms (``HT2025``), academic years
(``2024/25``) and calendar years (``2025``)."""
GEO_DIMENSIONS = frozenset({"Län", "Land"})
"""Breakdowns that are geographic areas: counties with their municipalities
(``Län[0];Kommun[1]``) and countries (``Land``). ``Land`` as a lower hierarchy
level (``Grupp[0];Land[1]``) belongs to a mixed dimension and gets no role."""


@dataclass
class PeriodFile:
    """Provenance of one period's export file."""

    url: str
    last_modified: str | None
    rows: int


@dataclass
class Scan:
    """Categories, markers and file provenance collected from an indicator's
    export files, one period at a time."""

    layout: Layout | None = None
    categories: list[dict[str, tuple[str, ...]]] = field(default_factory=list)
    """Per dimension: category code to hierarchy path, in first-seen order."""
    markers: set[str] = field(default_factory=set)
    files: dict[str, PeriodFile] = field(default_factory=dict)
    measure_columns: dict[str, str] = field(default_factory=dict)
    """Measure column headers, each with the newest period using it (UKÄ renamed
    some, e.g. ``AndelUbak`` to ``Andel``)."""
    period_columns: dict[str, str] = field(default_factory=dict)
    """Period column headers, each with the newest period using it. The period is
    always the first column; a few older exports misspell its header (``Lasar``,
    ``wÅr``)."""
    conflicts: dict[str, list[list[str]]] = field(default_factory=dict)
    """Per period, category combinations that UKÄ's export gives more than once
    with different values; retrieval refuses them."""

    def add(self, period: str, text: str, url: str, last_modified: str | None):
        """Add one period's export.

        The period column is identified by position, so its header may vary.

        Raises:
            ValueError: The export's breakdown columns differ from earlier
                periods', or a row is for another period.
        """
        layout, rows = read_export(text)
        if self.layout is None:
            self.layout = layout
            self.categories = [{} for _ in layout.dimensions]
        elif layout.dimensions != self.layout.dimensions:
            raise ValueError(
                f"export for {period} has layout {layout}, earlier periods "
                f"{self.layout}"
            )
        self.measure_columns.setdefault(layout.measure_column, period)
        self.period_columns.setdefault(layout.period_column, period)
        conflicting: set[tuple[str, ...]] = set()
        cells: dict[tuple[str, ...], tuple[Any, str | None]] = {}
        for row in rows:
            if row.period != period:
                raise ValueError(f"export for {period} has a row for {row.period!r}")
            for categories, path in zip(self.categories, row.paths, strict=True):
                if not path:
                    categories.setdefault(TOTAL, ())
                for depth in range(1, len(path) + 1):
                    categories.setdefault(category_code(path[:depth]), path[:depth])
            cell = (row.value, row.marker)
            if cells.setdefault(row.categories, cell) != cell:
                conflicting.add(row.categories)
            if row.marker is not None:
                self.markers.add(row.marker)
        if conflicting:
            self.conflicts[period] = [list(key) for key in sorted(conflicting)]
        self.files[period] = PeriodFile(url, last_modified, len(cells))


def _updated(files: dict[str, PeriodFile]) -> str | None:
    """The latest export ``Last-Modified`` date, as ``YYYY-MM-DD``."""
    dates = []
    for file in files.values():
        if not file.last_modified:
            continue
        try:
            dates.append(parsedate_to_datetime(file.last_modified).date().isoformat())
        except (TypeError, ValueError):
            continue
    return max(dates) if dates else None


def _dimension(
    layout: Layout, position: int, categories: dict[str, tuple[str, ...]]
) -> dict[str, Any]:
    dimension = layout.dimensions[position]
    codes = sorted(categories, key=lambda code: code != TOTAL)  # Total first
    category: dict[str, Any] = {
        "index": codes,
        "label": {
            code: (categories[code][-1] if categories[code] else TOTAL)
            for code in codes
        },
    }
    if dimension.hierarchical:
        children: dict[str, list[str]] = {}
        for code in codes:
            path = categories[code]
            if not path:
                continue
            parent = category_code(path[:-1])
            if parent in categories:
                children.setdefault(parent, []).append(code)
        if children:
            category["child"] = children
    result: dict[str, Any] = {
        "label": " / ".join(
            column.rsplit("[", 1)[0].strip() for column in dimension.columns
        ),
        "category": category,
    }
    if TOTAL in categories:
        result["extension"] = {
            "nordicintel": {"elimination": True, "elimination_value": TOTAL}
        }
    return result


def _roles(time: str, codes: tuple[str, ...]) -> dict[str, list[str]]:
    roles = {"time": [time]}
    if geo := [code for code in codes if code in GEO_DIMENSIONS]:
        roles["geo"] = geo
    return roles


def source_record(entry: Entry, indicator: Indicator) -> dict[str, Any]:
    """The accepted catalogue row and totals metadata: the indicator's source
    change signal besides its export links."""
    return {
        "catalogue": {
            "id": entry.id,
            "name": entry.name,
            "group": entry.group,
            "most_recent_url": entry.most_recent_url,
        },
        "totals": {
            "name": indicator.name,
            "description": indicator.description,
            "measurement": indicator.measurement,
            "current_year": indicator.current_year,
            "periods": list(indicator.periods),
        },
    }


def build_dataset(entry: Entry, indicator: Indicator, scan: Scan) -> dict[str, Any]:
    """Return the metadata-only Swedish Dataset document for one indicator.

    Raises:
        ValueError: No export was read, or a breakdown has no categories.
    """
    layout = scan.layout
    if layout is None or not scan.files:
        raise ValueError(f"indicator {indicator.id} has no readable export")
    periods = [period for period in indicator.periods if period in scan.files]
    time = layout.period_column
    dimension: dict[str, Any] = {time: {"label": time, "category": {"index": periods}}}
    for position, categories in enumerate(scan.categories):
        code = layout.dimensions[position].code
        if not categories:
            raise ValueError(f"indicator {indicator.id}: {code!r} has no categories")
        dimension[code] = _dimension(layout, position, categories)
    ids = [time, *layout.dimension_codes]

    shared: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": str(indicator.id),
        "language": LANGUAGE,
        "metadata_url": totals_url(indicator.id),
        "data_url": totals_url(indicator.id)
        + "?"
        + "&".join(f"{key}={value}" for key, value in EXPORT_PARAMS.items()),
    }
    if doc_url := documentation_url(indicator.description):
        shared["doc_url"] = doc_url
    if description := plain_text(indicator.description):
        shared["description"] = description
    if is_discontinued(indicator):
        shared["discontinued"] = True
    if time_unit := TIME_UNITS.get(time):
        shared["time_unit"] = time_unit
    shared["first_period"] = periods[0]
    shared["last_period"] = periods[-1]
    shared["subject"] = {"label": entry.group}

    provider: dict[str, Any] = {
        "indicator_name": indicator.name,
        "measurement": indicator.measurement,
        "current_year": indicator.current_year,
        "period_columns": list(scan.period_columns),
        "measure_columns": list(scan.measure_columns),
        "columns": {d.code: list(d.columns) for d in layout.dimensions},
        "markers": sorted(scan.markers),
        "conflicting_cells": {
            period: scan.conflicts[period]
            for period in periods
            if period in scan.conflicts
        },
        "files": {
            period: {
                "url": scan.files[period].url,
                "last_modified": scan.files[period].last_modified,
            }
            for period in periods
        },
        "source_record": source_record(entry, indicator),
    }

    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": entry.name,
    }
    if updated := _updated(scan.files):
        document["updated"] = updated
    document |= {
        "id": ids,
        "size": [len(dimension[code]["category"]["index"]) for code in ids],
        "role": _roles(time, layout.dimension_codes),
        "dimension": dimension,
        "value": [],
        "extension": {"nordicintel": shared, PROVIDER_CODE: provider},
    }
    return document

"""Tillväxtverket source model: the dataset registry and parsers for its responses.

The DivePort server has no listing endpoint (``/api/api/query`` and
``/api/api/query/inkvartering`` answer 404/400), so the datasets are a hard-coded
registry. Each entry names the columns of one ``.cbase`` file by role; a
dataset whose live columns differ from its entry fails as a metadata error
until the registry is updated. Adding or removing a dataset is a code change.
"""

import csv
import io
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from retrieval.data_adapters.tillvaxtverket import (
    COUNTY_COLUMN,
    LEVEL_COLUMN,
    MUNICIPALITY_COLUMN,
    NATIONAL,
    REGION_COLUMNS,
    SUPPRESSED,
    region_code,
)

LOAD_DATE = "LOAD_DATE"
COUNTY_NAME = "LAN_NAMN"
MUNICIPALITY_NAME = "KOMMUN_NAMN"
REGION_DISTINCT = (
    LEVEL_COLUMN,
    COUNTY_COLUMN,
    COUNTY_NAME,
    MUNICIPALITY_COLUMN,
    MUNICIPALITY_NAME,
)
TYPE_COLUMN = "ANLAGGNINGSTYP_NAMN"
DAY_TYPE_COLUMN = "DAGTYP_NAMN"
COUNTRY_COLUMN = "LAND_KOD"
COUNTRY_NAME = "LAND_NAMN"
COUNTRY_GROUPS = (
    "LANDGRP_SVUTL_NAMN",
    "LANDGRP_GROV_NAMN",
    "LANDGRP_20_NAMN",
    "LANDGRP_40_NAMN",
)
CAPACITY = (
    "ANTAL_GASTNATTER",
    "BELAGDA_BADDAR",
    "BELAGDA_RUM",
    "DISPONIBLA_BADDAR",
    "DISPONIBLA_RUM",
)
REVENUE = (
    "LOGIINTAKT",
    *CAPACITY,
    "ANKOMSTER_SVE",
    "ANKOMSTER_UTL",
    "ANTAL_ANLAGGNINGAR",
)
TIME_UNITS = {"AR": "annual", "PERIOD": "monthly", "FULL_DATUM": "daily"}
_LOAD_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}), \d{2}:\d{2}:\d{2}")


class Conflict(ValueError):
    """One identity (category code) arrived with two different descriptions; the
    dataset is quarantined (its last accepted document is kept)."""


@dataclass(frozen=True)
class Dimension:
    """A dimension read from one source column (besides Region and the measures)."""

    id: str
    column: str
    label_columns: tuple[str, ...] = ()
    """Columns describing each category: the first is its label, the rest are
    kept as namespaced category attributes."""


@dataclass(frozen=True)
class Spec:
    """One registered dataset: a ``.cbase`` file and its columns by role."""

    name: str
    time: str
    dimensions: tuple[Dimension, ...]
    measures: tuple[str, ...]
    attributes: tuple[str, ...] = ()
    """Columns derived from other columns (month names, week days, ...)."""
    area: str = "inkvartering"

    @property
    def dataset_code(self) -> str:
        return f"{self.area}/{self.name}"

    @property
    def time_unit(self) -> str:
        return TIME_UNITS[self.time]

    @property
    def columns(self) -> set[str]:
        return {
            LOAD_DATE,
            *REGION_COLUMNS,
            COUNTY_NAME,
            MUNICIPALITY_NAME,
            *(c for d in self.dimensions for c in (d.column, *d.label_columns)),
            self.time,
            *self.measures,
            *self.attributes,
        }


TYPE = Dimension("Anlaggningstyp", TYPE_COLUMN)
DAY_TYPE = Dimension("Dagtyp", DAY_TYPE_COLUMN)
COUNTRY = Dimension("Land", COUNTRY_COLUMN, (COUNTRY_NAME, *COUNTRY_GROUPS))
MONTH = ("AR", "MANAD_NUMMER")

DATASETS: tuple[Spec, ...] = (
    Spec(
        "GuestNights_Capacity_Day",
        "FULL_DATUM",
        (TYPE,),
        CAPACITY,
        (
            "AR",
            "PERIOD",
            "MANAD_LANG_SVE",
            "AR_VECKA",
            "DATUM_LANG",
            "VECKODAG_SVE",
            DAY_TYPE_COLUMN,
        ),
    ),
    Spec("GuestNights_Capacity_Year", "AR", (TYPE, DAY_TYPE), CAPACITY),
    Spec(
        "GuestNights_Capacity_Revenue_Month",
        "PERIOD",
        (TYPE,),
        REVENUE,
        (*MONTH, "MANAD_LANG_SVE"),
    ),
    Spec("GuestNights_Capacity_Revenue_Year", "AR", (TYPE,), REVENUE),
    Spec(
        "GuestNights_Country_Month",
        "PERIOD",
        (TYPE, COUNTRY),
        ("ANTAL_GASTNATTER",),
        (*MONTH, "MANAD_NAMN_LANG"),
    ),
    Spec("GuestNights_Country_Year", "AR", (TYPE, COUNTRY), ("ANTAL_GASTNATTER",)),
)
BY_NAME = {spec.name: spec for spec in DATASETS}
assert len({spec.name.casefold() for spec in DATASETS}) == len(DATASETS)


def text(value: Any) -> str:
    """A cell as category text: integral numbers without ``.0``."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


@dataclass(frozen=True)
class Probe:
    """The change signal of one dataset: its columns, row count and load times."""

    columns: tuple[tuple[str, str], ...]
    row_count: int
    load_dates: tuple[str, ...]

    def signal(self) -> dict[str, Any]:
        return {
            "columns": [{"name": n, "type": t} for n, t in self.columns],
            "row_count": self.row_count,
            "load_dates": list(self.load_dates),
        }

    @property
    def loaded(self) -> str | None:
        """The latest ``LOAD_DATE`` text (``2026-09-28, 20:17:21``)."""
        return max(self.load_dates) if self.load_dates else None

    @property
    def updated(self) -> str | None:
        """The calendar date of the latest load, when it parses."""
        match = _LOAD_DATE.fullmatch(self.loaded or "")
        return match.group(1) if match else None


def _rows(body: Any, what: str) -> list[Mapping[str, Any]]:
    if not isinstance(body, Mapping) or not isinstance(body.get("rows"), list):
        raise ValueError(f"{what}: response has no rows")
    rows = body["rows"]
    count = body.get("rowCount")
    if isinstance(count, int) and count != len(rows):
        raise ValueError(f"{what}: {len(rows)} of {count} rows returned")
    return rows


def parse_probe(spec: Spec, columns_body: Any, load_body: Any) -> Probe:
    """Parse the ``limit=0`` response and the ``dimension=LOAD_DATE`` response.

    Raises:
        ValueError: The columns differ from the registry entry, or a response
            is malformed.
    """
    if not isinstance(columns_body, Mapping) or not isinstance(
        columns_body.get("columns"), list
    ):
        raise ValueError("column response has no columns")
    columns = tuple(
        (str(column["name"]), str(column.get("type") or ""))
        for column in columns_body["columns"]
    )
    names = {name for name, _ in columns}
    if names != spec.columns:
        raise ValueError(
            f"columns changed: unexpected {sorted(names - spec.columns)}, "
            f"missing {sorted(spec.columns - names)}"
        )
    count = columns_body.get("rowCount")
    if not isinstance(count, int) or count < 0:
        raise ValueError(f"invalid rowCount {count!r}")
    loads = sorted({text(row.get(LOAD_DATE)) for row in _rows(load_body, LOAD_DATE)})
    return Probe(columns, count, tuple(load for load in loads if load))


@dataclass(frozen=True)
class Region:
    code: str
    label: str
    parent: str | None


def parse_regions(body: Any) -> list[Region]:
    """Region categories from the distinct region-column combinations.

    National rows are one ``riket`` category whatever their county/municipality
    filler; a county's merged suppressed municipalities are their own category.
    Order: Riket, counties by code, municipalities by county then code (the
    merged category last).

    Raises:
        ValueError: An unknown combination, or one code with two labels.
    """
    found: dict[str, Region] = {}
    for row in _rows(body, "regions"):
        level, county, municipality = (text(row.get(c)) for c in REGION_COLUMNS)
        code = region_code(level, county, municipality)
        if level == NATIONAL:
            region = Region(code, NATIONAL, None)
        elif code.startswith("lan:"):
            region = Region(code, text(row.get(COUNTY_NAME)) or county, "riket")
        else:
            label = (
                SUPPRESSED
                if municipality == SUPPRESSED
                else text(row.get(MUNICIPALITY_NAME)) or municipality
            )
            region = Region(code, label, f"lan:{county}")
        if found.setdefault(code, region) != region:
            raise Conflict(f"conflicting duplicate region {code!r}: {row}")

    def order(region: Region):
        kind = region.code.split(":")[0]
        rank = {"riket": 0, "lan": 1, "kommun": 2}[kind]
        parent = region.parent or ""
        return (rank, parent, region.code.endswith(SUPPRESSED), region.code)

    return sorted(found.values(), key=order)


@dataclass(frozen=True)
class Category:
    code: str
    label: str
    attributes: tuple[tuple[str, str], ...] = ()


def parse_categories(dimension: Dimension, body: Any) -> list[Category]:
    """Distinct values of one dimension column, sorted by code.

    Raises:
        ValueError: An empty code, or one code with differing descriptions.
    """
    found: dict[str, Category] = {}
    for row in _rows(body, dimension.column):
        code = text(row.get(dimension.column))
        if not code:
            raise ValueError(f"{dimension.column}: empty category code")
        label_columns = dimension.label_columns
        label = text(row.get(label_columns[0])) if label_columns else code
        attributes = tuple((c, text(row.get(c))) for c in label_columns[1:])
        category = Category(code, label or code, attributes)
        if found.setdefault(code, category) != category:
            raise Conflict(f"conflicting duplicate {dimension.column} {code!r}")
    return sorted(found.values(), key=lambda category: category.code)


def parse_periods(spec: Spec, body: Any) -> list[str]:
    """Distinct time codes, ascending."""
    periods = {text(row.get(spec.time)) for row in _rows(body, spec.time)}
    if "" in periods:
        raise ValueError(f"{spec.time}: empty period")
    return sorted(periods)


def parse_labels(content: str) -> dict[str, str]:
    """Column labels from a ``_variabeletiketter.csv`` file (``Name;…;Label``)."""
    reader = csv.DictReader(io.StringIO(content), delimiter=";")
    if not reader.fieldnames or not {"Name", "Label"} <= set(reader.fieldnames):
        raise ValueError("label file lacks Name/Label columns")
    return {
        row["Name"].strip(): row["Label"].strip()
        for row in reader
        if (row.get("Name") or "").strip() and (row.get("Label") or "").strip()
    }


def _first(values: Iterable[Any]) -> Any:
    return next(iter(values), None)


def parse_dcat(body: Any) -> dict[str, Any]:
    """The data.europa.eu fields the documents use, kept verbatim.

    Raises:
        ValueError: The record has no Swedish title.
    """
    record = body.get("result") if isinstance(body, Mapping) else None
    if not isinstance(record, Mapping):
        raise ValueError("data.europa.eu answered without a result")
    title = record.get("title") or {}
    if not str(title.get("sv") or "").strip():
        raise ValueError("data.europa.eu record has no Swedish title")
    description = record.get("description") or {}
    publisher = record.get("publisher") or {}
    distributions = []
    for item in record.get("distributions") or []:
        licence = item.get("license") or {}
        distributions.append(
            {
                "format": (item.get("format") or {}).get("id"),
                "access_url": _first(item.get("access_url") or []),
                "description_sv": (item.get("description") or {}).get("sv"),
                "license": licence.get("resource") or licence.get("id"),
                "modified": item.get("modified"),
                "page": [p.get("resource") for p in item.get("page") or []],
            }
        )
    distributions.sort(key=lambda item: str(item["format"]))
    translation = (record.get("translation_meta") or {}).get("details") or {}
    return {
        "id": record.get("id"),
        "title": {k: title[k] for k in ("sv", "en") if title.get(k)},
        "description": {k: description[k] for k in ("sv", "en") if description.get(k)},
        "english_machine_translated": (translation.get("en") or {}).get(
            "machine_translated"
        ),
        "publisher": {
            k: publisher[k] for k in ("name", "resource") if publisher.get(k)
        },
        "contact_point": [
            {k: v for k, v in contact.items() if k in ("name", "email", "resource")}
            for contact in record.get("contact_point") or []
        ],
        "page": [p.get("resource") for p in record.get("page") or []],
        "issued": record.get("issued"),
        "modified": record.get("modified"),
        "distributions": distributions,
    }

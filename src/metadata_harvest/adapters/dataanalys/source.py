"""Analysverktyg source model: areas, dashboards, catalogue rows and indicator cubes.

The site is an Apache Superset instance. Each indicator area ("OMR") has two
semantic tables: ``semantic_VAI_{AREA}_Landningssida``, the catalogue with one row
per indicator and geographic level, and ``semantic_VAI_{AREA}_Trender``, the values
in long format. The chart list names both; the dashboard list gives the public
pages that ``source_url`` links to.
"""

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

from metadata_harvest.adapters.dataanalys.api import (
    CATALOGUE_OPTIONAL,
    CATALOGUE_REQUIRED,
    LEVELS,
    SITE,
)

AREA_TABLE = re.compile(
    r"^(?i:semantic)\.semantic_VAI_(?P<area>[A-Za-z0-9]+)_"
    r"(?P<role>Landningssida|Trender)$"
)
"""Semantic area tables. The schema prefix is matched case-insensitively: NRTV's
catalogue is listed as ``Semantic.semantic_VAI_NRTV_Landningssida``."""
CATALOGUE, VALUES = "Landningssida", "Trender"
FILTER_SUBJECT = "Matt_id"
_DASHBOARD_PATH = re.compile(r"^/superset/dashboard/(?P<slug>[^/]+)/?$")


@dataclass(frozen=True)
class Area:
    """An indicator area with both semantic tables."""

    code: str
    catalogue_id: int
    catalogue_dataset: str
    values_id: int
    values_dataset: str


@dataclass(frozen=True)
class Dashboard:
    id: int
    slug: str
    title: str
    published: bool
    tags: frozenset[str]


@dataclass(frozen=True)
class Link:
    """How an indicator's web page was found (``href``, ``chart_filter``,
    ``area_overview`` or ``unresolved``)."""

    resolution: str
    dashboard: Dashboard | None = None


@dataclass
class Discovery:
    """The chart and dashboard listings, interpreted."""

    areas: dict[str, Area] = field(default_factory=dict)
    incomplete: dict[str, str] = field(default_factory=dict)
    """Areas whose tables are not both listed exactly once, with the reason."""
    unsupported: list[str] = field(default_factory=list)
    """Listed datasources outside the semantic area tables (``MART.*`` and others)."""
    dashboards: dict[int, Dashboard] = field(default_factory=dict)
    filters: dict[str, set[int]] = field(default_factory=dict)
    """Indicator code to the dashboards of charts filtering ``Matt_id`` on it."""
    dashboard_codes: dict[int, set[str]] = field(default_factory=dict)
    """Dashboard to every indicator code its charts filter on."""
    charts_count: int = 0
    charts_listed: int = 0
    charts_complete: bool = True
    dashboards_count: int = 0
    dashboards_listed: int = 0
    dashboards_complete: bool = True
    problems: list[tuple[str, Exception]] = field(default_factory=list)
    """Malformed listing rows as ``(resource, error)``."""

    @property
    def by_slug(self) -> dict[str, Dashboard]:
        return {d.slug: d for d in self.dashboards.values()}

    @property
    def rows_complete(self) -> bool:
        """Both listings were complete and every row was understood."""
        return self.charts_complete and self.dashboards_complete and not self.problems


def check_page(body: Any, resource: str) -> tuple[int, list]:
    """``(count, result)`` of one Superset list page.

    Raises:
        ValueError: The page does not have Superset's list shape.
    """
    if (
        not isinstance(body, Mapping)
        or type(body.get("count")) is not int
        or not isinstance(body.get("result"), list)
    ):
        raise ValueError(f"Unexpected {resource} listing page: {str(body)[:200]}")
    return body["count"], body["result"]


def listing_complete(rows: list, count: int) -> bool:
    """Every listed row arrived exactly once."""
    ids = [row.get("id") for row in rows if isinstance(row, Mapping)]
    return len(rows) == count and len(set(map(str, ids))) == len(ids) == count


def _form_data(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _filtered_codes(form_data: Mapping[str, Any]) -> list[str]:
    codes = []
    for item in form_data.get("adhoc_filters") or []:
        if (
            isinstance(item, Mapping)
            and item.get("subject") == FILTER_SUBJECT
            and item.get("operator") == "IN"
            and isinstance(item.get("comparator"), list)
        ):
            codes += [c for c in item["comparator"] if isinstance(c, str) and c]
    return codes


def add_charts(discovery: Discovery, rows: Iterable[Any]) -> None:
    """Interpret chart list rows: area tables, unsupported datasources, filters."""
    tables: dict[str, dict[str, set[tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(set)
    )
    unsupported = set()
    for position, row in enumerate(rows):
        chart_id = row.get("id") if isinstance(row, Mapping) else None
        name = row.get("datasource_name_text") if isinstance(row, Mapping) else None
        datasource = row.get("datasource_id") if isinstance(row, Mapping) else None
        dashboards = row.get("dashboards") if isinstance(row, Mapping) else None
        if (
            type(chart_id) is not int
            or not (name is None or isinstance(name, str))
            or not (datasource is None or type(datasource) is int)
            or not isinstance(dashboards or [], list)
            or any(
                not isinstance(d, Mapping) or type(d.get("id")) is not int
                for d in dashboards or []
            )
        ):
            discovery.problems.append(
                (
                    f"charts/{chart_id if type(chart_id) is int else f'#{position}'}",
                    ValueError(f"Malformed chart row {str(row)[:200]}"),
                )
            )
            continue
        if name:
            match = AREA_TABLE.match(name)
            if match and datasource is not None:
                tables[match["area"]][match["role"]].add((datasource, name))
            else:
                unsupported.add(name)
        dashboard_ids = [d["id"] for d in dashboards or []]
        for code in _filtered_codes(_form_data(row.get("form_data"))):
            discovery.filters.setdefault(code, set()).update(dashboard_ids)
            for dashboard_id in dashboard_ids:
                discovery.dashboard_codes.setdefault(dashboard_id, set()).add(code)
    for code, roles in sorted(tables.items()):
        if len(roles.get(CATALOGUE, ())) == 1 and len(roles.get(VALUES, ())) == 1:
            (catalogue_id, catalogue), (values_id, values) = (
                next(iter(roles[CATALOGUE])),
                next(iter(roles[VALUES])),
            )
            discovery.areas[code] = Area(
                code, catalogue_id, catalogue, values_id, values
            )
            continue
        reasons = []
        for role in (CATALOGUE, VALUES):
            listed = sorted(roles.get(role, ()))
            if not listed:
                reasons.append(f"no {role} table listed")
            elif len(listed) > 1:
                reasons.append(f"several {role} tables listed: {listed}")
        discovery.incomplete[code] = "; ".join(reasons)
    discovery.unsupported = sorted(unsupported)


def add_dashboards(discovery: Discovery, rows: Iterable[Any]) -> None:
    """Interpret dashboard list rows; rows without a slug are skipped."""
    for position, row in enumerate(rows):
        dashboard_id = row.get("id") if isinstance(row, Mapping) else None
        if (
            type(dashboard_id) is not int
            or not isinstance(row.get("published"), bool)
            or not isinstance(row.get("tags") or [], list)
            or not (row.get("slug") is None or isinstance(row.get("slug"), str))
        ):
            discovery.problems.append(
                (
                    "dashboards/"
                    + (
                        str(dashboard_id)
                        if type(dashboard_id) is int
                        else f"#{position}"
                    ),
                    ValueError(f"Malformed dashboard row {str(row)[:200]}"),
                )
            )
            continue
        slug = (row.get("slug") or "").strip()
        if not slug:
            continue
        tags = frozenset(
            str(tag["name"])
            for tag in row.get("tags") or []
            if isinstance(tag, Mapping) and isinstance(tag.get("name"), str)
        )
        discovery.dashboards[dashboard_id] = Dashboard(
            dashboard_id,
            slug,
            str(row.get("dashboard_title") or "").strip(),
            row["published"],
            tags,
        )


class _Anchor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.text: list[str] = []
        self._inside = False
        self._done = False

    def handle_starttag(self, tag, attrs):
        if tag == "a" and not self._done and self.href is None:
            self.href = dict(attrs).get("href")
            self._inside = True

    def handle_endtag(self, tag):
        if tag == "a" and self._inside:
            self._inside, self._done = False, True

    def handle_data(self, data):
        if self._inside:
            self.text.append(data)


def parse_anchor(html: str | None) -> tuple[str | None, str]:
    """The first link's ``href`` and text in ``HrefIndicatorDashboard``."""
    if not html:
        return None, ""
    parser = _Anchor()
    parser.feed(html)
    parser.close()
    return parser.href, " ".join("".join(parser.text).split())


def href_slug(href: str | None) -> str | None:
    """The dashboard slug of an analysverktyg dashboard URL, else None."""
    if not href:
        return None
    parts = urlsplit(href)
    if parts.netloc and parts.netloc.lower() != urlsplit(SITE).netloc:
        return None
    match = _DASHBOARD_PATH.match(parts.path)
    return unquote(match["slug"]) if match else None


def area_tag(area: str) -> str:
    return f"OMR: {area}"


def overview_dashboard(discovery: Discovery, area: str) -> Dashboard | None:
    """The published area start page (tags ``OMR: {area}`` and ``Start``)."""
    candidates = [
        d
        for d in discovery.dashboards.values()
        if d.published and {area_tag(area), "Start"} <= d.tags
    ]
    return min(candidates, key=lambda d: d.id) if candidates else None


def resolve_link(
    discovery: Discovery, area: str, code: str, href_html: str | None
) -> Link:
    """Find the published dashboard that is an indicator's web page.

    1. The catalogue link, when its slug is a listed published dashboard.
    2. Else a published dashboard of charts filtering ``Matt_id`` on the code:
       preferably tagged with the area, then the most specific (fewest filtered
       indicator codes), then the lowest id.
    3. Else the area's published start page.

    Raises:
        ValueError: The catalogue link is not listed and the dashboard listing was
            incomplete, so a fallback could be wrong.
    """
    slug = href_slug(parse_anchor(href_html)[0])
    dashboard = discovery.by_slug.get(slug) if slug else None
    if dashboard is not None and dashboard.published:
        return Link("href", dashboard)
    if not discovery.dashboards_complete:
        raise ValueError(
            f"catalogue link {slug!r} is not a listed dashboard and the dashboard "
            "listing is incomplete"
        )
    candidates = [
        discovery.dashboards[i]
        for i in discovery.filters.get(code, ())
        if i in discovery.dashboards and discovery.dashboards[i].published
    ]
    if candidates:
        return Link(
            "chart_filter",
            min(
                candidates,
                key=lambda d: (
                    area_tag(area) not in d.tags,
                    len(discovery.dashboard_codes.get(d.id, ())),
                    d.id,
                ),
            ),
        )
    if (overview := overview_dashboard(discovery, area)) is not None:
        return Link("area_overview", overview)
    return Link("unresolved")


@dataclass
class Catalogue:
    """One area's catalogue rows at the ``Riket`` level."""

    rows: list[tuple[str, dict[str, Any]]]
    """Valid rows with a unique ``MBusinessKey``, in source order."""
    duplicates: list[str]
    """Codes listed more than once: quarantined."""
    problems: list[tuple[int, Exception]]
    """Malformed rows by position."""
    columns: list[str]
    """The catalogue columns requested (optional ones the table lacks dropped)."""

    @property
    def complete(self) -> bool:
        return not self.problems


def parse_catalogue(rows: list[dict[str, Any]], columns: list[str]) -> Catalogue:
    """Validate catalogue rows; a malformed row never stops later rows."""
    found: dict[str, list[dict[str, Any]]] = {}
    problems: list[tuple[int, Exception]] = []
    for position, row in enumerate(rows):
        code = row.get("MBusinessKey")
        error = None
        if not isinstance(code, str) or not code.strip():
            error = "MBusinessKey is not a nonblank string"
        elif code != code.strip() or "|" in code or "/" in code:
            error = f"MBusinessKey {code!r} has surrounding space, '|' or '/'"
        elif not (
            row.get("DataGenerationDate") is None
            or (
                isinstance(row["DataGenerationDate"], (int, float))
                and not isinstance(row["DataGenerationDate"], bool)
                and math.isfinite(row["DataGenerationDate"])
            )
        ):
            error = "DataGenerationDate is not epoch milliseconds"
        elif bad := [
            name
            for name in (*CATALOGUE_REQUIRED, *CATALOGUE_OPTIONAL)
            if name not in ("MBusinessKey", "DataGenerationDate")
            and not (row.get(name) is None or isinstance(row.get(name), str))
        ]:
            error = f"non-text values in {bad}"
        if error:
            problems.append(
                (
                    position,
                    ValueError(f"Malformed catalogue row: {error}: {str(row)[:200]}"),
                )
            )
            continue
        found.setdefault(code, []).append(row)
    return Catalogue(
        rows=[
            (code, matches[0]) for code, matches in found.items() if len(matches) == 1
        ],
        duplicates=sorted(code for code, matches in found.items() if len(matches) > 1),
        problems=problems,
        columns=list(columns),
    )


@dataclass(frozen=True)
class Cube:
    """The distinct dimension categories of one indicator."""

    regions: tuple[tuple[str, str], ...]
    """``(code, level)`` with the ``Riket`` level first, then sorted by code."""
    sexes: tuple[str, ...] | None
    ages: tuple[str, ...] | None
    periods: tuple[str, ...]
    """In ``Matperiod_datum`` order."""
    period_types: tuple[str, ...]


def _text(row: Mapping[str, Any], column: str, what: str) -> str:
    value = row.get(column)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} ({column}) is not a nonblank string: {value!r}")
    return value


def _optional(rows, column: str | None, what: str) -> list[str] | None:
    """Distinct codes of an optional dimension column; None when absent or all null."""
    if column is None:
        return None
    values = [row.get(column) for row in rows]
    if all(value is None for value in values):
        return None
    return sorted({_text(row, column, what) for row in rows})


def _date_key(value: Any) -> tuple:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, str) and value.strip():
        return (1, 0.0, value)
    return (2, 0.0, "")


def _natural(text: str) -> tuple:
    match = re.match(r"\d+", text)
    return (int(match.group()) if match else math.inf, text)


def leading(codes: list[str], first: str) -> list[str]:
    """``codes`` with ``first`` moved to the front when present."""
    return [first, *(c for c in codes if c != first)] if first in codes else codes


SEX_TOTAL, AGE_TOTAL = "Kön, totalt", "Ålder, totalt"


def parse_cube(rows: list[dict[str, Any]], columns: Mapping[str, str]) -> Cube | None:
    """Interpret the distinct dimension rows; None when there are none.

    Raises:
        ValueError: A row has a missing code, a level outside the harvested ones, or a
            label at two levels.
    """
    if not rows:
        return None
    levels: dict[str, str] = {}
    for row in rows:
        level = _text(row, columns["level"], "level")
        if level not in LEVELS:
            raise ValueError(f"unexpected level {level!r}")
        code = _text(row, columns["code"], "region")
        if levels.setdefault(code, level) != level:
            raise ValueError(f"region {code!r} listed at two levels")
    regions = sorted(levels.items(), key=lambda item: (LEVELS.index(item[1]), item[0]))
    dates: dict[str, tuple] = {}
    for row in rows:
        period = _text(row, columns["period"], "period")
        key = (
            _date_key(row.get(columns["period_date"]))
            if "period_date" in columns
            else (2, 0.0, "")
        )
        dates[period] = min(dates.get(period, key), key)
    periods = sorted(dates, key=lambda period: (dates[period], period))
    sexes = _optional(rows, columns.get("sex"), "sex")
    ages = _optional(rows, columns.get("age"), "age")
    types = sorted(
        {
            row.get(columns["period_type"])
            for row in rows
            if isinstance(row.get(columns["period_type"]), str)
            and row[columns["period_type"]].strip()
        }
        if "period_type" in columns
        else set()
    )
    return Cube(
        regions=tuple(regions),
        sexes=tuple(leading(sexes, SEX_TOTAL)) if sexes else None,
        ages=tuple(leading(sorted(ages, key=_natural), AGE_TOTAL)) if ages else None,
        periods=tuple(periods),
        period_types=tuple(types),
    )

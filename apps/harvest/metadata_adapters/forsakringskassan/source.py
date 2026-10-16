"""Försäkringskassan source model: the catalogue, report ``meta.json`` and texts.

``/reports`` lists benefit groups (``key``, the ``forman``) in areas, each with its
report keys. A report's ``meta.json`` lists every dimension with its complete
category list, the measures (``filter.matt``) and one to five tables
(``filter.uppdelning``, "breakdowns"). A dimension with a ``group`` belongs only to
the table whose breakdown key equals that group; the others belong to every table.
Each table becomes one NordicIntel Dataset (see the adapter guide).
"""

import hashlib
import html
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from apps.harvest.metadata_adapters.forsakringskassan.client import (
    MEASURE_DIMENSION,
    ROW_COUNTER,
    SITE,
)

logger = logging.getLogger("forsakringskassan_logger")

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
"""Report, table, dimension and measure keys: safe in URLs and dataset codes."""


def fingerprint(value: Any) -> str:
    """SHA-256 of the canonical JSON form (sorted keys, compact separators)."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Catalogue ------------------------------------------------------------------


@dataclass(frozen=True)
class Listed:
    """One report as the catalogue lists it."""

    report: str
    area: str
    group: str

    def record(self) -> dict[str, str]:
        return {"report": self.report, "area": self.area, "group": self.group}


def catalogue(body: Any, problems: list) -> tuple[list[Listed], bool]:
    """Valid, unique reports of ``/reports``; malformed rows and reports listed more
    than once are reported in ``problems`` and make the listing incomplete.

    Raises:
        ValueError: The response is not a list of groups at all.
    """
    if not isinstance(body, list):
        raise ValueError(f"unexpected /reports response {str(body)[:200]!r}")
    complete, found = True, {}
    for number, group in enumerate(body):
        if not (
            isinstance(group, dict)
            and isinstance(group.get("area"), str)
            and _KEY.match(group["area"])
            and isinstance(group.get("key"), str)
            and _KEY.match(group["key"])
            and isinstance(group.get("reports"), list)
        ):
            problems.append(
                (f"#{number}", ValueError(f"Malformed group {group!r}"[:300]))
            )
            complete = False
            continue
        for report in group["reports"]:
            if not (isinstance(report, str) and _KEY.match(report)):
                problems.append(
                    (
                        f"{group['key']}#{number}",
                        ValueError(f"Malformed report {report!r}"),
                    )
                )
                complete = False
                continue
            found.setdefault(report, []).append(
                Listed(report, group["area"], group["key"])
            )
    listed = []
    for report, matches in found.items():
        if len(matches) > 1:
            problems.append((report, ValueError("Listed more than once")))
            complete = False
        else:
            listed.append(matches[0])
    logger.debug(
        "Catalogue: %d groups, %d unique reports, %d problems, complete=%s",
        len(body),
        len(listed),
        len(problems),
        complete,
    )
    return listed, complete


# --- meta.json --------------------------------------------------------------------


@dataclass(frozen=True)
class Category:
    code: str
    label: str


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    categories: tuple[Category, ...]
    group: str | None


@dataclass(frozen=True)
class Measure:
    key: str
    label: str


@dataclass(frozen=True)
class Table:
    """One breakdown table of a report: the unit of one Dataset."""

    key: str
    """Breakdown key (``uppdelning_lan``), matched by dimension ``group``."""
    label: str
    name: str
    """Table name used in the data URL (``ASEBeslutLan``)."""
    dimensions: tuple[str, ...]
    """Dimension keys of the table, in report ``filter.order``."""


@dataclass(frozen=True)
class Report:
    key: str
    area: str
    forman: str
    official: bool | None
    version: str | None
    revision: Any
    dimensions: dict[str, Dimension]
    measures: tuple[Measure, ...]
    tables: tuple[Table, ...]
    duplicate_tables: tuple[str, ...]
    computed: tuple[Any, ...]
    undeclared: tuple[str, ...]
    """Columns in ``table.order`` that are neither dimensions, measures nor computed
    (such as ``kommun_kod``): some table's rows carry a dimension ``meta.json`` does
    not describe."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _key(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise ValueError(f"invalid {what} key {value!r}")
    return value


def parse_meta(meta: Any, report: str, area: str) -> Report:
    """Parse one report's ``meta.json``.

    Raises:
        ValueError: The document is malformed, names another report, or has a
            dimension or measure clash.
    """
    if not isinstance(meta, dict) or not isinstance(meta.get("filter"), dict):
        raise ValueError("meta.json has no filter object")
    if meta.get("key") != report:
        raise ValueError(f"meta.json describes {meta.get('key')!r}, not {report!r}")
    source = meta["filter"]
    dimensions: dict[str, Dimension] = {}
    for item in source.get("dimension") or []:
        key = _key(item.get("key") if isinstance(item, dict) else None, "dimension")
        if key in dimensions or key in (MEASURE_DIMENSION, ROW_COUNTER):
            raise ValueError(f"duplicate or reserved dimension key {key!r}")
        categories = tuple(
            Category(_text(value.get("key")), _text(value.get("label")))
            for value in item.get("values") or []
            if isinstance(value, dict)
        )
        codes = [category.code for category in categories]
        if not codes or "" in codes or len(set(codes)) != len(codes):
            raise ValueError(f"dimension {key!r} has missing or duplicate categories")
        group = item.get("group")
        dimensions[key] = Dimension(
            key, _text(item.get("label")), categories, _text(group) or None
        )
    measures = tuple(
        Measure(_key(item.get("key"), "measure"), _text(item.get("label")))
        for item in source.get("matt") or []
        if isinstance(item, dict)
    )
    if not measures or len({m.key for m in measures}) != len(measures):
        raise ValueError("report has no measures or duplicate measures")
    order = [
        key
        for key in source.get("order") or []
        if isinstance(key, str) and key in dimensions
    ]
    order += [key for key in dimensions if key not in order]
    tables, seen, duplicates = [], {}, []
    for item in source.get("uppdelning") or []:
        if not isinstance(item, dict):
            raise ValueError(f"malformed breakdown {item!r}")
        name = _key(item.get("table"), "table")
        breakdown = _text(item.get("key"))
        seen[name] = seen.get(name, 0) + 1
        members = tuple(
            key
            for key in order
            if dimensions[key].group is None or dimensions[key].group == breakdown
        )
        if not members:
            raise ValueError(f"table {name!r} has no dimensions")
        tables.append(Table(breakdown, _text(item.get("label")), name, members))
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if not tables:
        raise ValueError("report lists no tables")
    official = meta.get("official")
    computed = tuple(source.get("computed") or ())
    known = {
        *dimensions,
        *(m.key for m in measures),
        *(c.get("key") for c in computed if isinstance(c, dict)),
    }
    columns = (meta.get("table") or {}).get("order") or []
    undeclared = tuple(
        dict.fromkeys(c for c in columns if isinstance(c, str) and c not in known)
    )
    logger.debug(
        "Parsed meta.json of %s: %d dimensions, %d measures, %d tables "
        "(%d duplicate names), undeclared columns %s",
        report,
        len(dimensions),
        len(measures),
        len(tables),
        len(duplicates),
        list(undeclared),
    )
    return Report(
        key=report,
        area=_text(meta.get("area")) or area,
        forman=_key(meta.get("forman"), "forman"),
        official=official if isinstance(official, bool) else None,
        version=_text(meta.get("version")) or None,
        revision=meta.get("revision"),
        dimensions=dimensions,
        measures=measures,
        tables=tuple(t for t in tables if t.name not in duplicates),
        duplicate_tables=tuple(duplicates),
        computed=computed,
        undeclared=undeclared,
    )


# --- Texts --------------------------------------------------------------------------


def text_entries(texts: Mapping[str, Any], report: Report) -> dict[str, Any]:
    """The text entries one report's documents use (its change signal)."""
    return {
        name: texts.get(name)
        for name in (
            f"MEKA.report.{report.key}",
            f"MEKA.forman.{report.forman}",
            f"MEKA.area.{report.area}",
        )
    }


def unescape(value: Any) -> str:
    """Unescape the texts' HTML, which is escaped twice (``&amp;lt;p&amp;gt;``)."""
    return html.unescape(html.unescape(_text(value)))


def plain(value: Any) -> str:
    """Plain text of a (double-escaped) HTML fragment, whitespace collapsed."""
    parser = _Plain()
    parser.feed(unescape(value))
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


class _Plain(HTMLParser):
    _BLOCKS = {"p", "br", "li", "h1", "h2", "h3", "h4", "ul", "ol", "div", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self._BLOCKS:
            self.parts.append(" ")

    def handle_endtag(self, tag) -> None:
        if tag in self._BLOCKS:
            self.parts.append(" ")

    def handle_data(self, data) -> None:
        self.parts.append(data)


@dataclass
class About:
    """What the ``about`` text says: sections by heading, links and e-mail."""

    sections: dict[str, dict[str, str]]
    """``h2`` heading to ``h3`` heading (``""`` before the first) to plain text."""
    links: list[tuple[str, str]]
    """Document links (PDF) as (absolute URL, label), in text order, unique."""
    emails: list[str]


class _AboutParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: dict[str, dict[str, list[str]]] = {"": {"": []}}
        self.h2, self.h3 = "", ""
        self.heading: list[str] | None = None
        self.heading_tag = ""
        self.anchor: dict[str, str] | None = None
        self.anchor_text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.emails: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag in ("h2", "h3"):
            self.heading, self.heading_tag = [], tag
        elif tag == "a":
            self.anchor, self.anchor_text = dict(attrs), []
        elif tag in _Plain._BLOCKS:
            self._add(" ")

    def handle_endtag(self, tag) -> None:
        if tag == self.heading_tag and self.heading is not None:
            title = re.sub(r"\s+", " ", "".join(self.heading)).strip()
            if tag == "h2":
                self.h2, self.h3 = title, ""
            else:
                self.h3 = title
            self.sections.setdefault(self.h2, {}).setdefault(self.h3, [])
            self.heading = None
        elif tag == "a" and self.anchor is not None:
            href = (self.anchor.get("href") or "").strip()
            label = re.sub(r"\s+", " ", "".join(self.anchor_text)).strip()
            if href.lower().startswith("mailto:"):
                email = href[7:].split("?", 1)[0].strip()
                if email and email not in self.emails:
                    self.emails.append(email)
            elif href and href.lower().split("?", 1)[0].endswith(".pdf"):
                url = urljoin(SITE + "/", href)
                if url.startswith(("https://", "http://")) and all(
                    url != known for known, _ in self.links
                ):
                    self.links.append((url, label or url))
            self.anchor = None
        elif tag in _Plain._BLOCKS:
            self._add(" ")

    def handle_data(self, data) -> None:
        if self.heading is not None:
            self.heading.append(data)
            return
        if self.anchor is not None:
            self.anchor_text.append(data)
        self._add(data)

    def _add(self, data: str) -> None:
        self.sections.setdefault(self.h2, {}).setdefault(self.h3, []).append(data)


def parse_about(value: Any) -> About:
    parser = _AboutParser()
    parser.feed(unescape(value))
    parser.close()
    sections = {
        h2: {
            h3: text
            for h3, parts in subsections.items()
            if (text := re.sub(r"\s+", " ", "".join(parts)).strip()) or h3
        }
        for h2, subsections in parser.sections.items()
    }
    return About(
        {h2: s for h2, s in sections.items() if s}, parser.links, parser.emails
    )


MEASURES_HEADING = "Mått"

_UNIT = re.compile(
    r"(?:\b1\s?000-tals?|\btusentals?|\bmiljoner|\bmiljontals)\s+(?:kronor|kr)\b"
    r"|\b(?:kronor|kr)\b|%|\bprocent\b",
    re.IGNORECASE,
)


def stated_unit(*texts: str) -> str | None:
    """A unit phrase stated verbatim in a measure's own label or heading, if any.

    Only the phrase is taken (``Belopp i 1000-tal kr`` gives ``1000-tal kr``,
    ``Andel mottagare per kön (%)`` gives ``%``); nothing is inferred from measure
    keys or other text. Texts are tried in order; the first match wins.
    """
    for text in texts:
        if match := _UNIT.search(text or ""):
            return match.group(0)
    return None


# --- Dimension probe ------------------------------------------------------------------


def probe_filters(report: Report, table: Table) -> dict[str, list[str]]:
    """A one-row filter for a table: ``ALL`` or else the first category of each
    declared dimension (the newest year, as years are listed newest first)."""
    filters = {}
    for key in table.dimensions:
        codes = [category.code for category in report.dimensions[key].categories]
        filters[key] = ["ALL" if "ALL" in codes else codes[0]]
    return filters


def undeclared_dimensions(rows: Any, table: Table) -> list[str]:
    """Row keys of a probe response that ``meta.json`` does not declare.

    Raises:
        ValueError: The response is malformed, has no rows (inconclusive) or lacks
            a declared dimension.
    """
    if not isinstance(rows, list):
        raise ValueError(f"unexpected probe response {str(rows)[:200]!r}")
    if not rows:
        raise ValueError(
            "probe returned no rows; the table's dimensions are unconfirmed"
        )
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("dimensions"), dict):
            raise ValueError(f"malformed probe row {str(row)[:200]!r}")
        keys |= set(row["dimensions"]) - {ROW_COUNTER}
    if missing := sorted(set(table.dimensions) - keys):
        raise ValueError(f"probe rows lack declared dimensions {missing}")
    extra = sorted(keys - set(table.dimensions))
    logger.debug(
        "Probe of table %s: %d rows, undeclared row keys %s",
        table.name,
        len(rows),
        extra,
    )
    return extra

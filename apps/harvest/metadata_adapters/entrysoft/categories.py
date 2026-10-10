"""Find every category of every dimension column of a RowStore table in few requests.

RowStore has no "distinct values" endpoint. It does offer:
- pages of at most 500 rows with ``resultCount`` (total matches) in every response,
- per-column filters that are regular expressions when they start with ``^``
  (PostgreSQL ARE: case-sensitive, lookahead supported), combined with AND.

Strategy
--------
1. The first unfiltered page (500 rows) gives the row count, column names and a
   sample. If the table fits in it, every category is known after one request.
2. Columns are classified as dimensions or measures (``classify``). Only dimension
   categories are enumerated; measures become categories of a synthetic metric
   dimension.
3. Each dimension, largest first, is enumerated by asking only for rows whose
   value is *not yet known*: ``^(?!(?:v1|v2|...)$)``. Every returned row carries
   an unseen value for that column, and every row also adds values for all other
   columns, so later dimensions usually need only one confirming request.
4. Completeness is proven without an extra request: when a response's
   ``resultCount`` fits in the page just returned, every remaining value has been
   seen.
5. The exclusion list can outgrow the URL limit (~8 KB). Known values are then
   split into disjoint prefixes where they branch (a long shared prefix such as
   "Kommun nummer " is taken whole), each prefix is enumerated with its own short
   exclusion list (``^Kommun nummer 0(?!(?:01|02|...)$)``), and one final query
   confirms that nothing exists outside those prefixes
   (``^(?!(?:Kommun nummer 0|Kommun nummer 1))...``).
6. Any table larger than one page is first read from the files it was built from
   (its ``dcterms:source`` distribution): one request for their list, one per file.
   They are used only if their row count equals the table's and they contain every
   value of the first page. Enumeration and full scans are the fallback. A full
   scan costs ``ceil(rowcount / 500)`` requests. Every enumeration response
   bounds what finishing its query can cost (one page per 500 rows still
   matching), so enumeration switches to a full scan, which completes every column
   at once, as soon as that bound exceeds the remaining full scan. A dimension that
   is unique per row in the first page goes straight to a full scan. Total cost is therefore at most about twice a
   full scan, and usually a small fraction of it.

All of this is exact: categories are complete by construction, not estimated.
Only the dimension/measure classification is heuristic; pass ``roles`` to override.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from math import ceil

from apps.harvest.metadata_adapters.entrysoft.client import MAX_URL_LENGTH, Client
from apps.harvest.metadata_adapters.entrysoft.source import load_source_rows

logger = logging.getLogger("entrysoft_logger")

PAGE_SIZE = 500
NEAR_UNIQUE_SHARE = 0.98  # sample share of distinct values that triggers a full scan
_REGEX_SPECIAL = set("\\^$.|?*+()[]{}")
_CLASS_SPECIAL = set("\\]^-[")

MISSING_MARKERS = {"", "..", ".", "-", "–", "x", "X", ":"}
_NUMBER = re.compile(r"^[-−]?(?:\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)?$")
# Name hints that a numeric column is a dimension, not a measure.
_DIMENSION_SUBSTRINGS = (
    "year",
    "month",
    "week",
    "date",
    "datum",
    "period",
    "quarter",
    "kvartal",
    "vecka",
    "manad",
    "månad",
    "kommun",
    "region",
    "county",
    "kategori",
    "category",
    "grupp",
    "group",
    "alder",
    "ålder",
    "klass",
    "sektor",
    "bransch",
    "omrade",
    "område",
    "kod",
    "code",
    "nummer",
    "number",
    "time",
    "tidpunkt",
    "län",
)
_DIMENSION_TOKENS = {
    "ar",
    "år",
    "id",
    "nr",
    "lan",
    "kon",
    "kön",
    "sex",
    "age",
    "typ",
    "tid",
    "dag",
    "day",
}
_TIME_SUBSTRINGS = (
    "year",
    "month",
    "week",
    "date",
    "datum",
    "period",
    "quarter",
    "kvartal",
    "vecka",
    "manad",
    "månad",
    "time",
    "tidpunkt",
)
_TIME_TOKENS = {"ar", "år", "tid", "dag", "day"}


class _UseFullScan(Exception):
    """Enumeration would cost more than reading the remaining pages."""


class EmptyTable(ValueError):
    """The table has no rows, so it has no categories to describe."""


class NeedsPaging(Exception):
    """The table is larger than one page, its source files cannot be used, and
    paging was not allowed. Carries the reason and what paging would cost."""

    def __init__(self, reason: str, rowcount: int, estimated_requests: int):
        super().__init__(reason)
        self.reason, self.rowcount, self.estimated_requests = (
            reason,
            rowcount,
            estimated_requests,
        )


def regex_escape(text: str) -> str:
    return "".join("\\" + char if char in _REGEX_SPECIAL else char for char in text)


def _class_escape(chars) -> str:
    return "".join(
        "\\" + char if char in _CLASS_SPECIAL else char for char in sorted(chars)
    )


def _exclusion(rests: list[str]) -> str:
    """Lookaheads rejecting exactly the given remainders (``""`` = nothing left)."""
    alternatives = [regex_escape(rest) for rest in rests if rest]
    pattern = "(?!(?:" + "|".join(alternatives) + ")$)" if alternatives else ""
    return pattern + ("(?!$)" if "" in rests else "")


def prefix_pattern(prefix: str, seen) -> str:
    """Regex for values starting with ``prefix`` that are not in ``seen``."""
    rests = sorted(value[len(prefix) :] for value in seen if value.startswith(prefix))
    return "^" + regex_escape(prefix) + _exclusion(rests)


def remainder_pattern(prefixes: list[str], seen) -> str:
    """Regex for values under none of ``prefixes`` and not in ``seen``."""
    others = sorted(
        value for value in seen if not any(value.startswith(p) for p in prefixes)
    )
    pattern = "^"
    if prefixes:
        pattern += "(?!(?:" + "|".join(regex_escape(p) for p in sorted(prefixes)) + "))"
    return pattern + _exclusion(others)


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    length = 0
    while length < min(len(low), len(high)) and low[length] == high[length]:
        length += 1
    return low[:length]


def plan_prefixes(prefix: str, rests: list[str], fits) -> list[str] | None:
    """Split known values into disjoint prefixes whose exclusion queries fit a URL.

    Splits only where values branch: a shared prefix is taken whole (so a long
    common prefix costs no extra requests). Returns ``None`` if a single value
    cannot fit.
    """
    if fits(prefix_pattern(prefix, [prefix + rest for rest in rests])):
        return [prefix]
    shared = _common_prefix([rest for rest in rests if rest]) if "" not in rests else ""
    if shared:
        return plan_prefixes(
            prefix + shared, [rest[len(shared) :] for rest in rests], fits
        )
    groups: dict[str, list[str]] = {}
    for rest in rests:
        if rest:
            groups.setdefault(rest[0], []).append(rest[1:])
    if not groups:
        return None
    planned: list[str] = []
    for char in sorted(groups):
        sub = plan_prefixes(prefix + char, groups[char], fits)
        if sub is None:
            return None
        planned.extend(sub)
    return planned


def _tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[^0-9a-zåäöüé]+", name.lower()) if token]


def _hinted(name: str, substrings, tokens) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in substrings) or any(
        token in tokens for token in _tokens(name)
    )


def clean_name(column: str) -> str:
    """Column name without a byte-order mark or surrounding whitespace."""
    return column.replace("﻿", "").strip()


def classify(columns: list[str], rows: list[dict], roles: dict | None = None) -> dict:
    """Classify each column as a dimension or a measure from its name and sample values."""
    result = {}
    for column in columns:
        if roles and column in roles:
            result[column] = {"role": roles[column], "reason": "override"}
            continue
        name = clean_name(column)
        values = [str(row.get(column, "")).strip() for row in rows]
        present = [value for value in values if value not in MISSING_MARKERS]
        if _hinted(name, _DIMENSION_SUBSTRINGS, _DIMENSION_TOKENS):
            result[column] = {"role": "dimension", "reason": "name"}
        elif present and all(_NUMBER.match(value) for value in present):
            result[column] = {"role": "measure", "reason": "numeric values"}
        else:
            result[column] = {"role": "dimension", "reason": "text values"}
        result[column]["time"] = result[column]["role"] == "dimension" and _hinted(
            name, _TIME_SUBSTRINGS, _TIME_TOKENS
        )
    return result


def natural_key(value: str):
    # ASCII digits only: str.isdigit() is also true for "²", which int() rejects.
    return [
        (0, int(part), "") if part.isascii() and part.isdigit() else (1, 0, part)
        for part in re.split(r"([0-9]+)", value)
    ]


@dataclass
class _State:
    client: Client
    table_url: str
    total: int
    values: dict[str, set[str]]
    budget: int  # requests a full scan would still need
    enumeration_requests: int = 0
    pending: int = (
        0  # dimensions still to enumerate after the current one (>= 1 request each)
    )
    log: list[dict] = field(default_factory=list)

    def add(self, rows: list[dict]) -> None:
        for row in rows:
            for column, value in row.items():
                self.values.setdefault(column, set()).add(
                    "" if value is None else str(value)
                )


async def _request(state: _State, column: str, pattern: str, label: str) -> dict:
    if state.enumeration_requests + 1 + state.pending > state.budget:
        logger.debug(
            "%s: enumerating %r would exceed the %d-request full scan "
            "(%d used, %d dimensions pending); switching to full scan",
            state.table_url,
            column,
            state.budget,
            state.enumeration_requests,
            state.pending,
        )
        raise _UseFullScan
    state.enumeration_requests += 1
    before = len(state.values[column])
    page = await state.client.get(
        state.table_url, {column: pattern, "_limit": PAGE_SIZE}
    )
    state.add(page.get("results") or [])
    state.log.append(
        {
            "column": column,
            "query": label,
            "matches": page.get("resultCount"),
            "new": len(state.values[column]) - before,
        }
    )
    # Rows still matching this query bound what finishing it can cost (one page per
    # 500 of them at most), and every pending dimension needs at least one request.
    # Switch as soon as that projection exceeds a full scan.
    remaining = page["resultCount"] - len(page.get("results") or [])
    projected = state.enumeration_requests + ceil(remaining / PAGE_SIZE) + state.pending
    logger.debug(
        "%s: %r query %s matched %s rows, %d new values",
        state.table_url,
        column,
        label,
        page.get("resultCount"),
        state.log[-1]["new"],
    )
    if remaining > 0 and projected > state.budget:
        logger.debug(
            "%s: finishing %r would cost up to %d requests against a %d-request "
            "full scan; switching to full scan",
            state.table_url,
            column,
            projected,
            state.budget,
        )
        raise _UseFullScan
    return page


def _done(page: dict) -> bool:
    """True when the page holds every row matching its query."""
    return page["resultCount"] <= len(page.get("results") or [])


async def _enumerate(state: _State, column: str) -> None:
    """Request rows with unseen values of ``column`` until none remain."""

    def fits(pattern: str) -> bool:
        return (
            state.client.url_length(
                state.table_url, {column: pattern, "_limit": PAGE_SIZE}
            )
            <= MAX_URL_LENGTH
        )

    completed: list[str] = []  # prefixes whose values are all known
    while True:
        seen = state.values[column]
        planned = plan_prefixes("", sorted(seen), fits)
        if planned is None:
            logger.debug(
                "%s: known values of %r cannot be split into URL-sized prefixes; "
                "switching to full scan",
                state.table_url,
                column,
            )
            raise _UseFullScan
        replan = False
        for prefix in planned:
            if any(prefix.startswith(done) for done in completed):
                continue
            while True:
                pattern = prefix_pattern(prefix, state.values[column])
                if not fits(pattern):
                    replan = True  # this prefix's known values outgrew the URL
                    break
                before = len(state.values[column])
                page = await _request(state, column, pattern, f"prefix {prefix!r}")
                if _done(page):
                    completed.append(prefix)
                    break
                if len(state.values[column]) == before:
                    raise RuntimeError(f"Enumeration of {column!r} made no progress")
            if replan:
                break
        if replan:
            continue
        if "" in completed:
            return  # the whole value space is complete
        pattern = remainder_pattern(completed, state.values[column])
        if not fits(pattern):
            logger.debug(
                "%s: remainder query for %r exceeds the URL limit; "
                "switching to full scan",
                state.table_url,
                column,
            )
            raise _UseFullScan
        page = await _request(state, column, pattern, "remainder")
        if _done(page):
            return
        # New values outside every planned prefix: plan again with them included.


async def _full_scan(state: _State) -> None:
    logger.debug(
        "%s: full scan of %d rows (%d further requests)",
        state.table_url,
        state.total,
        state.budget,
    )
    fetched = PAGE_SIZE
    for offset in range(PAGE_SIZE, state.total, PAGE_SIZE):
        page = await state.client.get(
            state.table_url, {"_limit": PAGE_SIZE, "_offset": offset}
        )
        if page.get("resultCount") != state.total:
            raise ValueError("Table row count changed during the scan")
        rows = page.get("results") or []
        state.add(rows)
        fetched += len(rows)
    if fetched < state.total:
        raise ValueError(f"Full scan returned {fetched} of {state.total} rows")


def _matching_source_values(
    columns: list[str], sample: list[dict], source: list[dict], total: int
) -> tuple[dict[str, set[str]] | None, str]:
    """Values per RowStore column from the source rows, if they provably match the table."""
    if len(source) != total:
        return None, f"source files have {len(source)} rows, the table {total}"
    # RowStore lowercases column names ("Län" in the file becomes "län").
    by_clean = {clean_name(name).casefold(): name for name in source[0]}
    mapping = {
        column: by_clean.get(clean_name(column).casefold()) for column in columns
    }
    missing = [column for column, name in mapping.items() if name is None]
    if missing:
        return None, f"source files lack columns {missing[:5]}"
    values = {
        column: {"" if row.get(name) is None else row[name] for row in source}
        for column, name in mapping.items()
    }
    for row in sample:
        for column in columns:
            if str(row.get(column, "")) not in values[column]:
                return (
                    None,
                    f"value {str(row.get(column))[:40]!r} of column {column!r} is not in the source files",
                )
    return values, "ok"


async def _from_source(
    state: _State, source_uri: str | None, sample: list[dict]
) -> str:
    """Take every value from the table's source files when cheaper than paging and verified.

    Returns ``"used"`` or the reason the files were not used.
    """
    if not source_uri:
        return "no source distribution"
    if state.budget < 2:
        return "paging is cheaper"
    # Unexpected failures (bad responses, parse errors) propagate: they are bugs,
    # not reasons to quietly read the table some other, more expensive way.
    rows, reason = await load_source_rows(
        state.client, source_uri, max_files=state.budget - 1
    )
    if rows is None:
        return reason
    values, reason = _matching_source_values(
        list(state.values), sample, rows, state.total
    )
    if values is None:
        return reason
    state.values.update(values)
    return "used"


async def infer_structure(
    client: Client,
    table_url: str,
    *,
    roles: dict | None = None,
    source_uri: str | None = None,
    first_page: dict | None = None,
    max_paging_requests: int | None = None,
) -> dict:
    """Return columns, roles and the complete category list of every dimension.

    A table larger than one page is read from its source files when possible.
    Otherwise it is paged (enumeration or full scan) only if that would take at
    most ``max_paging_requests`` requests (``None``: no limit); beyond that,
    ``NeedsPaging`` is raised instead of spending the requests.

    Raises:
        EmptyTable: The table has no rows.
        NeedsPaging: Paging is needed but not allowed.
        ValueError: For an invalid response or a table that changes while being read.
    """
    start = client.requests
    # A caller that already fetched the first page (to learn the row count) passes it in.
    first = first_page or await client.get(table_url, {"_limit": PAGE_SIZE})
    total, rows = first.get("resultCount"), first.get("results") or []
    if total == 0:
        logger.debug("%s: empty table", table_url)
        raise EmptyTable(f"Empty RowStore table: {table_url}")
    if not isinstance(total, int) or total < 0 or not rows:
        logger.error(
            "%s: invalid RowStore response (resultCount=%r, %d rows)",
            table_url,
            total,
            len(rows),
        )
        raise ValueError(f"Invalid RowStore table response: {table_url}")
    columns: list[str] = []
    for row in rows:
        columns.extend(column for column in row if column not in columns)

    state = _State(
        client,
        table_url,
        total,
        {column: set() for column in columns},
        ceil(total / PAGE_SIZE) - 1,
    )
    state.add(rows)
    roles_by_column = classify(columns, rows, roles)
    dimensions = [
        column for column in columns if roles_by_column[column]["role"] == "dimension"
    ]

    logger.debug(
        "%s: %d rows, %d columns, %d dimensions; first page holds %d rows",
        table_url,
        total,
        len(columns),
        len(dimensions),
        len(rows),
    )
    strategy, source_check = "single_page", None
    if total > len(rows):
        # Files first: the files a table was built from cost one request each,
        # which beats paging or enumerating anything larger than one page.
        source_check = await _from_source(state, source_uri, rows)
        if source_check == "used":
            strategy = "source_files"
            logger.debug("%s: categories read from the source files", table_url)
        elif (
            dimensions
            and max_paging_requests is not None
            and state.budget > max_paging_requests
        ):
            logger.debug(
                "%s: source files not used (%s); paging needs %d requests, "
                "more than the %d allowed; deferring",
                table_url,
                source_check,
                state.budget,
                max_paging_requests,
            )
            raise NeedsPaging(source_check, total, state.budget)
        else:
            logger.debug(
                "%s: source files not used (%s); paging needs up to %d requests",
                table_url,
                source_check,
                state.budget,
            )
    if strategy == "single_page" and total > len(rows) and dimensions:
        # A dimension that is nearly unique per row in the sample would need about
        # one request per 500 rows to enumerate: no cheaper than a full scan.
        near_unique = [
            c
            for c in dimensions
            if len(state.values[c]) >= NEAR_UNIQUE_SHARE * len(rows)
        ]
        strategy = "full_scan" if near_unique else "enumeration"
        if near_unique:
            logger.debug(
                "%s: dimensions %s are nearly unique per row; going straight to "
                "a full scan",
                table_url,
                near_unique,
            )
        try:
            if near_unique:
                raise _UseFullScan
            # Largest first: a column too costly to enumerate is caught on its first
            # probe (one wasted request), and its rows fill in the other columns.
            order = sorted(dimensions, key=lambda c: -len(state.values[c]))
            for position, column in enumerate(order):
                state.pending = len(order) - position - 1
                await _enumerate(state, column)
        except _UseFullScan:
            strategy = "full_scan"
            await _full_scan(state)

    logger.debug(
        "%s: structure inferred with strategy %s in %d requests",
        table_url,
        strategy,
        client.requests - start,
    )
    sample_keys = [tuple(row.get(column) for column in dimensions) for row in rows]
    return {
        "rowcount": total,
        "columns": columns,
        "roles": roles_by_column,
        "dimensions": dimensions,
        "measures": [column for column in columns if column not in dimensions],
        "categories": {
            column: sorted(state.values[column], key=natural_key)
            for column in dimensions
        },
        "strategy": strategy,
        "source_check": source_check,
        "requests": client.requests - start,
        "full_scan_requests": ceil(total / PAGE_SIZE),
        # False when the dimension columns do not identify rows, e.g. respondent-level
        # survey tables or registers: the categories are exact, but the table is not a cube.
        "dimension_key_unique_in_sample": len(set(sample_keys)) == len(sample_keys),
        "enumeration_log": state.log,
    }

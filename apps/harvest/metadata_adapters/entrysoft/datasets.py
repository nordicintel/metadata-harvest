"""Turn harvested RowStore tables into the datasets we expose.

A RowStore table is not a dataset. Two things get in the way:

* Columns. A supplier ledger has ``verifikationsnummer`` (one value per row),
  ``kommun_id`` (one value for the whole table) and ``konto_nr``/``konto_text``
  (the same thing twice). None of those are dimensions anyone filters on.
* Tables. Härryda publishes its ledger as 27 tables (one per month or year) with
  identical columns; Transportstyrelsen splits one dataset into one table per
  city. Those are one dataset each, with a ``part`` dimension.

Everything here is decided from the harvested structure alone, so a new table
gets the same treatment without anyone looking at it. The rules, in order:

Columns
    constant   one distinct value             -> dropped, kept under ``constants``
    identifier distinct >= IDENTIFIER_SHARE   -> dropped, kept under ``identifiers``
               of rows and the column holds
               codes (numeric values or a name
               like ``id``/``nr``/``nummer``);
               a near-unique text column such as
               ``myndighet`` stays a dimension;
               when nothing else is left the
               identifiers are the dimensions
    time, dense                               -> time dimension given as a range
               (more than MAX_LISTED time
               values)
    paired     same distinct count as another -> one dimension; the code-looking
               column, names share a stem        column is the code, the other
                                                 the label
    wide       more than MAX_LISTED values    -> dimension kept, categories not
                                                 listed (``enumerable: false``)
    otherwise                                 -> dimension with every category

Tables
    Tables of one provider with identical columns whose titles differ only by
    a period (year, month, quarter, "Januari - December 2022") or that belong
    to the same catalogue dataset are merged into one dataset. The differing
    text becomes the ``part`` dimension; each category records its table URL.
    A table whose title says it is a test is dropped.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger("entrysoft_logger")

MAPPING_VERSION = 1
"""Increment when a rule change must regroup every accepted table offline."""
MAX_LISTED = 1000  # categories listed in metadata; above this only the count
SAMPLE = 200  # category values kept for columns above MAX_LISTED
IDENTIFIER_SHARE = 0.5  # distinct/rows above which a code-like column is an identifier
MIN_ROWS_FOR_IDENTIFIER = 50
PART_DIMENSION = "part"

MONTHS = (
    "januari februari mars april maj juni juli augusti september oktober november december "
    "jan feb mar apr jun jul aug sep sept okt nov dec "
    "january february march may june july august october december"
).split()
_PERIOD = re.compile(
    r"\b(?:(?:19|20)\d{2}(?:[-/](?:19|20)?\d{2})?|"
    + "|".join(MONTHS)
    + r"|q[1-4]|kvartal\s*[1-4]|h[12]|t[1-3])\b",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[\s\-–—_:,./()]+")
_CODE_NAME = re.compile(
    r"(?:^|[_ ])(?:id|nr|nummer|kod|code|key)$|^(?:id|kod|code)(?:[_ ]|$)",
    re.IGNORECASE,
)
_CODE_VALUE = re.compile(r"^[\d\s.:/+-]+$|^[0-9a-f]{8}-[0-9a-f]{4}-")
_DATE_VALUE = re.compile(r"^(?:19|20)\d{2}(?:-\d{2}(?:-\d{2}(?:[ T]\d{2}:\d{2})?)?)?$")
_TRAILING_WORDS = re.compile(r"\s+(?:för|for|år|year|from|från|per)$", re.IGNORECASE)
_NOISE_WORDS = re.compile(r"(api|json|csv|xml|ny|new)", re.IGNORECASE)
_TEST_TITLE = re.compile(r"\btest\b", re.IGNORECASE)


def _title(value) -> str:
    """Catalogue titles are language maps; prefer Swedish."""
    if isinstance(value, dict):
        return value.get("sv") or value.get("en") or next(iter(value.values()), "")
    return str(value) if value else ""


def stem_and_period(text: str) -> tuple[str, str]:
    """Split a title into what stays the same across parts and what varies.

    ``"Leverantörsreskontra 2024 Januari - September"`` -> ``("leverantörsreskontra",
    "2024 januari september")``.
    """
    periods = [match.group(0).lower() for match in _PERIOD.finditer(text)]
    remainder = _PERIOD.sub(" ", text)
    stem = " ".join(_SEPARATORS.split(remainder.lower())).strip()
    return stem, " ".join(periods)


def _column_stem(name: str) -> str:
    return (
        _SEPARATORS.sub(" ", _CODE_NAME.sub("", name.lower()))
        .replace("text", "")
        .replace("namn", "")
        .strip()
    )


@dataclass
class Column:
    name: str
    distinct: int
    categories: list[str] | None
    time: bool
    raw: dict  # the harvested dimension entry, for provenance


@dataclass
class TableView:
    """One table after column rules; what the dataset merge works on."""

    document: dict
    dimensions: list[dict]
    measures: list[str]
    identifiers: list[str] = field(default_factory=list)
    constants: dict[str, str] = field(default_factory=dict)

    @property
    def signature(self) -> tuple[str, ...]:
        return tuple(sorted(self.document["extension"]["rowstore"]["columns"]))


def _columns(document: dict) -> list[Column]:
    columns = []
    for dimension_id, entry in document["dimension"].items():
        if dimension_id == "measure":
            continue
        categories = list(entry["category"]["index"])
        meta = entry["extension"]["rowstore"]
        # The harvest flags time by column name; ``start``/``slut`` holding timestamps
        # are only recognisable by their values.
        time = bool(meta.get("time")) or _mostly(categories, _DATE_VALUE)
        # A stored wide column keeps only a sample; ``distinct`` holds the true count.
        distinct = meta.get("distinct", len(categories))
        columns.append(Column(meta["column"], distinct, categories, time, entry))
    return columns


def trim(document: dict) -> dict:
    """Keep a sample, the count and the range of wide columns; drop the rest.

    The dataset rules need no more than this, so stored table structures stay
    small whatever the table size.
    """
    for dimension_id, entry in document["dimension"].items():
        categories = list(entry["category"]["index"])
        if dimension_id == "measure" or len(categories) <= MAX_LISTED:
            continue
        ordered = sorted(categories)
        sample = categories[:SAMPLE]
        entry["category"] = {
            "index": {value: position for position, value in enumerate(sample)},
            "label": {value: value for value in sample},
        }
        meta = entry.setdefault("extension", {}).setdefault("rowstore", {})
        meta.update(
            distinct=len(categories), listed=False, range=[ordered[0], ordered[-1]]
        )
    return document


def _pair(columns: list[Column]) -> dict[str, Column]:
    """Map label columns to the code column they duplicate.

    Two columns with the same number of distinct values whose names share a
    stem (``konto_nr``/``konto_text``, ``leverantor``/``leverantor_id``) describe
    the same dimension. The harvest's first page should confirm the 1:1 mapping
    once this runs inside the adapter; here only names and counts are available.
    """
    by_count: dict[int, list[Column]] = defaultdict(list)
    for column in columns:
        if column.distinct > 1:
            by_count[column.distinct].append(column)
    pairs = {}
    for group in by_count.values():
        for code in group:
            if not _CODE_NAME.search(code.name):
                continue
            for label in group:
                if (
                    label is not code
                    and label.name not in pairs
                    and _column_stem(label.name) == _column_stem(code.name)
                ):
                    pairs[label.name] = code
                    break
    return pairs


def _mostly(values: list[str], pattern: re.Pattern) -> bool:
    sample = [v for v in values[:200] if v.strip()]
    return bool(sample) and sum(
        bool(pattern.match(v.strip())) for v in sample
    ) >= 0.9 * len(sample)


def _dimension(column: Column, label_column: Column | None) -> dict:
    listed = column.distinct <= MAX_LISTED
    entry = {
        "id": column.raw.get("label") or column.name,
        "column": column.name,
        "distinct": column.distinct,
        "enumerable": listed,
        "time": column.time,
    }
    if label_column is not None:
        entry["label_column"] = label_column.name
    if column.time and not listed:
        ordered = column.raw["extension"]["rowstore"].get("range") or sorted(
            column.categories
        )
        entry["range"] = [ordered[0], ordered[-1]]
    elif listed:
        entry["categories"] = column.categories
        if label_column is not None and len(label_column.categories) == column.distinct:
            # Same order is not guaranteed; labels are matched live by the adapter.
            entry["labels_available"] = True
    return entry


def apply_column_rules(document: dict) -> TableView:
    """Reduce a harvested table to the dimensions worth querying."""
    rowcount = document["extension"]["rowstore"]["rowcount"]
    columns = _columns(document)
    view = TableView(
        document=document,
        dimensions=[],
        measures=list(
            document["dimension"]
            .get("measure", {})
            .get("category", {})
            .get("index", {})
        ),
    )
    pairs = _pair(columns)
    paired_labels = {label.name: label for label in columns if label.name in pairs}
    for column in columns:
        if column.distinct == 1:
            view.constants[column.name] = column.categories[0]
        elif column.name in pairs:
            continue  # folded into its code column
        elif (
            not column.time
            and rowcount > MIN_ROWS_FOR_IDENTIFIER
            and column.distinct >= IDENTIFIER_SHARE * rowcount
            and (
                _CODE_NAME.search(column.name)
                or _mostly(column.categories, _CODE_VALUE)
            )
        ):
            view.identifiers.append(column.name)
        else:
            label = next(
                (paired_labels[name] for name, code in pairs.items() if code is column),
                None,
            )
            view.dimensions.append(_dimension(column, label))
    if not view.dimensions and view.identifiers:
        # One row per entity (Skatteverket's rents: one row per värdeområde, one
        # column per year): the key column is the dimension, not noise.
        logger.debug(
            "%s: no dimension left after column rules; identifiers %s become "
            "the dimensions",
            document["extension"]["metadata_harvest"]["table_url"],
            view.identifiers,
        )
        view.dimensions = [
            _dimension(column, None)
            for column in columns
            if column.name in view.identifiers
        ]
        view.identifiers = []
    logger.debug(
        "%s: %d dimensions, %d identifiers, %d constants",
        document["extension"]["metadata_harvest"]["table_url"],
        len(view.dimensions),
        len(view.identifiers),
        len(view.constants),
    )
    return view


def _part_key(view: TableView) -> tuple:
    """Tables that merge: same provider, same columns, same title stem (or same catalogue dataset)."""
    record = view.document["extension"]["metadata_harvest"]
    catalogue = record["catalogue"]
    stem, _ = stem_and_period(
        _title(catalogue.get("dataset_title"))
        or record["title"]
        or record["dataset_code"]
    )
    return (record["provider_code"], view.signature, stem)


def _period_label(view: TableView) -> str:
    record = view.document["extension"]["metadata_harvest"]
    catalogue = record["catalogue"]
    tokens: list[str] = []
    for text in (
        _title(catalogue.get("dataset_title")) or record["title"] or "",
        _title(catalogue.get("distribution_title")),
    ):
        for token in stem_and_period(text)[1].split():
            if token not in tokens:
                tokens.append(token)
    return " ".join(tokens)


def _part_labels(members: list[TableView]) -> list[str]:
    """One label per table: its period when that tells them apart, else its distribution title."""
    labels = [_period_label(m) for m in members]
    shared = set.intersection(*(set(label.split()) for label in labels))
    labels = [" ".join(t for t in label.split() if t not in shared) for label in labels]
    if not (all(labels) and len(set(labels)) == len(labels)):
        labels = [
            " ".join(
                _NOISE_WORDS.sub(
                    " ",
                    _title(
                        m.document["extension"]["metadata_harvest"]["catalogue"].get(
                            "distribution_title"
                        )
                    ),
                ).split()
            )
            or m.document["extension"]["metadata_harvest"]["dataset_code"]
            for m in members
        ]
    unique: list[str] = []
    for label in labels:
        while label in unique:
            label += " ·"
        unique.append(label)
    return unique


def build_datasets(documents: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group table documents into datasets. Returns ``(datasets, dropped)``."""
    views, dropped = [], []
    for document in documents:
        table_url = document["extension"]["metadata_harvest"]["table_url"]
        if _TEST_TITLE.search(str(document["label"])):
            dropped.append({"table_url": table_url, "reason": "test table"})
            logger.debug("%s: dropped (test table)", table_url)
            continue
        if document["extension"]["rowstore"]["rowcount"] < 2:
            dropped.append({"table_url": table_url, "reason": "single row"})
            logger.debug("%s: dropped (single row)", table_url)
            continue
        views.append(apply_column_rules(document))
    logger.info(
        "Dataset rules: %d accepted tables, %d dropped (%s)",
        len(documents),
        len(dropped),
        ", ".join(f"{n} {r}" for r, n in Counter(d["reason"] for d in dropped).items())
        or "none",
    )

    # Union tables that share a title stem or a catalogue dataset (Transportstyrelsen
    # splits one dataset into differently named tables with the same columns).
    parent: dict[tuple, tuple] = {}

    def find(key):
        while parent.setdefault(key, key) != key:
            key = parent[key]
        return key

    for view in views:
        record = view.document["extension"]["metadata_harvest"]
        parent[
            find(
                (
                    "dataset",
                    record["provider_code"],
                    view.signature,
                    record["dataset_code"],
                )
            )
        ] = find(_part_key(view))
    groups: dict[tuple, list[TableView]] = defaultdict(list)
    for view in views:
        groups[find(_part_key(view))].append(view)

    datasets = []
    for members in groups.values():
        first = members[0]
        provider, _, stem = _part_key(first)
        record = first.document["extension"]["metadata_harvest"]
        dataset = {
            "provider_code": provider,
            "dataset_code": record["dataset_code"]
            if len(members) == 1
            else f"{provider}:{stem}",
            "title": record["title"] if len(members) == 1 else _shared_title(members),
            "tables": [
                m.document["extension"]["metadata_harvest"]["table_url"]
                for m in members
            ],
            "rowcount": sum(
                m.document["extension"]["rowstore"]["rowcount"] for m in members
            ),
            "dimensions": _merge_dimensions(members),
            "measures": first.measures,
            "kind": "cube" if any(m.dimensions for m in members) else "list",
            "identifiers": sorted({name for m in members for name in m.identifiers}),
            "constants": _shared_constants(members),
            "catalogue": record["catalogue"],
            "updated": max(
                (
                    m.document["extension"]["metadata_harvest"]["catalogue"].get(
                        "distribution_modified"
                    )
                    or m.document.get("updated")
                    or ""
                    for m in members
                ),
                default="",
            ),
        }
        if len(members) > 1:
            logger.debug(
                "Dataset %s merges %d tables with identical columns",
                dataset["dataset_code"],
                len(members),
            )
            parts = dict(zip(_part_labels(members), dataset["tables"], strict=True))
            dataset["dimensions"].insert(
                0,
                {
                    "id": PART_DIMENSION,
                    "distinct": len(parts),
                    "enumerable": True,
                    "time": False,
                    "categories": list(parts),
                    "tables": parts,
                },
            )
            dataset["signature"] = list(first.signature)
        datasets.append(dataset)
    # Two merged groups of one provider can share a title stem with different
    # columns; each then carries a digest of its columns so identities stay unique.
    by_code = defaultdict(list)
    for dataset in datasets:
        by_code[(dataset["provider_code"], dataset["dataset_code"])].append(dataset)
    for group in by_code.values():
        if len(group) > 1:
            logger.info(
                "Dataset code %s/%s is shared by %d groups with different columns; "
                "appending column digests",
                group[0]["provider_code"],
                group[0]["dataset_code"],
                len(group),
            )
            for dataset in group:
                digest = hashlib.sha256(
                    "|".join(dataset.get("signature", [])).encode()
                ).hexdigest()[:8]
                dataset["dataset_code"] += "#" + digest
    datasets.sort(key=lambda d: (d["provider_code"], d["dataset_code"]))
    logger.info(
        "Grouped %d tables into %d datasets (%d merged from several tables, "
        "%d without a usable dimension)",
        len(views),
        len(datasets),
        sum(1 for d in datasets if len(d["tables"]) > 1),
        sum(1 for d in datasets if d["kind"] != "cube"),
    )
    return datasets, dropped


def _shared_title(members: list[TableView]) -> str:
    title = _title(
        members[0]
        .document["extension"]["metadata_harvest"]["catalogue"]
        .get("dataset_title")
    )
    return _TRAILING_WORDS.sub(
        "", " ".join(_PERIOD.sub(" ", title).split()).strip(" -–:,")
    )


def _shared_constants(members: list[TableView]) -> dict[str, str | list[str]]:
    """Columns constant in every table; the value per table when it differs between them."""
    names = set.intersection(*(set(m.constants) for m in members))
    return {
        name: sorted({m.constants[name] for m in members})
        if len({m.constants[name] for m in members}) > 1
        else members[0].constants[name]
        for name in sorted(names)
    }


def _merge_dimensions(members: list[TableView]) -> list[dict]:
    """Union of each dimension's categories across the merged tables.

    A column that is an identifier or a constant in one table is not a dimension
    of the dataset either: a per-row number stays per-row in the other months.
    """
    excluded = {name for m in members for name in m.identifiers} | set.intersection(
        *(set(m.constants) for m in members)
    )
    merged: dict[str, dict] = {}
    for member in members:
        for dimension in member.dimensions:
            if dimension["column"] in excluded:
                continue
            current = merged.get(dimension["column"])
            if current is None:
                merged[dimension["column"]] = dict(dimension)
                continue
            if "categories" in current and "categories" in dimension:
                seen = set(current["categories"])
                current["categories"] += [
                    c for c in dimension["categories"] if c not in seen
                ]
                current["distinct"] = len(current["categories"])
            else:
                current["distinct"] = max(current["distinct"], dimension["distinct"])
                current["enumerable"] = False
                current.pop("categories", None)
            if "range" in current and "range" in dimension:
                current["range"] = [
                    min(current["range"][0], dimension["range"][0]),
                    max(current["range"][1], dimension["range"][1]),
                ]
    for dimension in merged.values():
        if dimension.get("distinct", 0) > MAX_LISTED and "categories" in dimension:
            dimension.pop("categories")
            dimension["enumerable"] = False
    return list(merged.values())

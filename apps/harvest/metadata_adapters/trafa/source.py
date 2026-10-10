"""Trafikanalys source model: products, their items and API combinations.

A Trafikanalys product is not one cube: which variables can be queried together
depends on the measures chosen. The product's API documentation page lists the
valid sets as "API-kombinationer"; each maximal combination with at least one
measure becomes one NordicIntel Dataset (see README, "Mapping decisions").
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

TIME = "Time"
REGION = "Region"


@dataclass(frozen=True)
class Category:
    """A variable value (``DV`` item)."""

    code: str
    label: str
    description: str


@dataclass(frozen=True)
class Hierarchy:
    """An ``H`` item grouping nested variables; it is not queryable itself."""

    name: str
    label: str
    description: str


@dataclass(frozen=True)
class Variable:
    """A queryable variable (``D`` item)."""

    name: str
    label: str
    description: str
    data_type: str
    categories: tuple[Category, ...]
    hierarchy: Hierarchy | None = None
    parent: str | None = None
    """Name of the enclosing variable when nested inside a hierarchy."""


@dataclass(frozen=True)
class Measure:
    """A measure (``M`` item)."""

    name: str
    label: str
    description: str


@dataclass(frozen=True)
class Product:
    """One Trafikanalys product in one language, parsed from ``/structure``."""

    name: str
    unique_id: str
    label: str
    description: str
    active_from: str | None
    variables: dict[str, Variable] = field(default_factory=dict)
    measures: dict[str, Measure] = field(default_factory=dict)


@dataclass(frozen=True)
class Combination:
    """A maximal API combination: the item set behind one Dataset.

    Attributes:
        product_id: Product ``UniqueId``.
        measures: Measure names, sorted.
        variables: Variable names, sorted.
        listed: The combinations as printed on the documentation page that this one
            covers (itself and its subsets), in page order.
    """

    product_id: str
    measures: tuple[str, ...]
    variables: tuple[str, ...]
    listed: tuple[str, ...]

    @property
    def dataset_code(self) -> str:
        """Stable dataset code: ``UniqueId|measures…|variables…``, names sorted."""
        return "|".join((self.product_id, *self.measures, *self.variables))


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _items(items: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    return list(items or [])


def product_list(structure: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return ``Name``, ``UniqueId``, ``Label`` and ``ActiveFrom`` of each product."""
    return [
        {
            "name": _text(item["Name"]),
            "unique_id": _text(item["UniqueId"]),
            "label": _text(item.get("Label")),
            "active_from": _text(item.get("ActiveFrom")),
        }
        for item in _items(structure.get("StructureItems"))
        if item.get("Type") == "P"
    ]


def parse_product(structure: Mapping[str, Any], name: str) -> Product:
    """Parse ``/structure?query=<name>`` into a :class:`Product`.

    The response repeats the product list; the product's own ``D``, ``M`` and ``H``
    items are its siblings with ``ParentName`` equal to the product name. ``H`` items
    hold nested ``D`` items, whose ``ParentName`` names the hierarchy or an
    enclosing variable. ``F`` items (special filters such as ``senaste``) are not
    categories and are skipped.

    Raises:
        LookupError: The product is not in the response.
    """
    items = _items(structure.get("StructureItems"))
    header = next(
        (item for item in items if item.get("Type") == "P" and item["Name"] == name),
        None,
    )
    if header is None:
        raise LookupError(f"product {name!r} missing from /structure response")
    product = Product(
        name=name,
        unique_id=_text(header["UniqueId"]),
        label=_text(header.get("Label")),
        description=_text(header.get("Description")),
        active_from=_text(header.get("ActiveFrom")) or None,
    )

    def add_variable(item: Mapping[str, Any], hierarchy: Hierarchy | None) -> None:
        parent = _text(item.get("ParentName"))
        variable = Variable(
            name=_text(item["Name"]),
            label=_text(item.get("Label")),
            description=_text(item.get("Description")),
            data_type=_text(item.get("DataType")) or "String",
            categories=tuple(
                Category(
                    _text(child["Name"]),
                    _text(child.get("Label")),
                    _text(child.get("Description")),
                )
                for child in _items(item.get("StructureItems"))
                if child.get("Type") == "DV"
            ),
            hierarchy=hierarchy,
            parent=parent if hierarchy and parent != hierarchy.name else None,
        )
        if variable.name in product.variables or variable.name in product.measures:
            raise ValueError(f"{name}: duplicate item name {variable.name!r}")
        product.variables[variable.name] = variable

    for item in items:
        kind = item.get("Type")
        if kind == "P" or _text(item.get("ParentName")) != name:
            continue
        if kind == "D":
            add_variable(item, None)
        elif kind == "M":
            measure = Measure(
                _text(item["Name"]),
                _text(item.get("Label")),
                _text(item.get("Description")),
            )
            product.measures[measure.name] = measure
        elif kind == "H":
            hierarchy = Hierarchy(
                _text(item["Name"]),
                _text(item.get("Label")),
                _text(item.get("Description")),
            )
            for child in _items(item.get("StructureItems")):
                if child.get("Type") == "D":
                    add_variable(child, hierarchy)
    return product


class _CombinationParser(HTMLParser):
    """Collect the query strings under ``<ul class="api-combos">``.

    Each ``<li>`` holds a ``<div class="name">`` with labels and a second ``<div>``
    with the query string, such as ``T10011|itrfslut|ar|bussklass``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found_list = False
        self.combinations: list[str] = []
        self._depth = 0  # element depth inside the list; 0 = outside
        self._capture: list[str] | None = None
        self._capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if self._depth == 0:
            if tag == "ul" and "api-combos" in classes:
                self.found_list = True
                self._depth = 1
            return
        self._depth += 1
        if tag == "div" and "name" not in classes and self._capture is None:
            self._capture = []
            self._capture_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0:
            return
        if self._capture is not None and self._depth == self._capture_depth:
            if text := "".join(self._capture).strip():
                self.combinations.append(text)
            self._capture = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture.append(data)


def parse_combinations(html: str) -> list[str]:
    """Return the API combination query strings listed on a documentation page.

    Raises:
        ValueError: The page has no API combination list.
    """
    parser = _CombinationParser()
    parser.feed(html)
    parser.close()
    if not parser.found_list:
        raise ValueError("documentation page has no API combination list")
    return parser.combinations


@dataclass
class Selection:
    """Combinations chosen as datasets, plus the listed ones that were not."""

    datasets: list[Combination]
    without_measures: list[str]
    """Listed combinations without any measure: they return no observations."""


def select_combinations(product: Product, listed: Sequence[str]) -> Selection:
    """Choose the datasets of a product from its listed API combinations.

    Names are matched case-insensitively against the product's items. Duplicates
    (the same item set in another order) collapse; combinations without a measure
    are set aside; the rest reduce to the maximal item sets, because a subset is
    the same query with some variables eliminated (omitted).

    Raises:
        ValueError: A combination names another product or an unknown item.
    """
    sets: dict[frozenset[str], list[str]] = {}
    without_measures: list[str] = []
    for text in listed:
        product_id, *names = text.split("|")
        if product_id.strip().casefold() != product.unique_id.casefold():
            raise ValueError(f"combination {text!r} is not for {product.unique_id}")
        normalized = frozenset(name.strip().lower() for name in names)
        if unknown := sorted(
            normalized - product.variables.keys() - product.measures.keys()
        ):
            raise ValueError(f"combination {text!r} names unknown items {unknown}")
        if not normalized & product.measures.keys():
            without_measures.append(text)
            continue
        sets.setdefault(normalized, []).append(text)

    datasets = []
    for items in sets:
        if any(items < other for other in sets):
            continue
        covered = [
            text for other, group in sets.items() if other <= items for text in group
        ]
        datasets.append(
            Combination(
                product_id=product.unique_id,
                measures=tuple(sorted(items & product.measures.keys())),
                variables=tuple(sorted(items & product.variables.keys())),
                listed=tuple(text for text in listed if text in covered),
            )
        )
    datasets.sort(key=lambda combination: combination.dataset_code)
    return Selection(datasets, without_measures)

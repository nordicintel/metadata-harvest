"""Map SDB subjects, variables and categories to NordicIntel Dataset documents."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from metadata_harvest.adapters.sdb.retrieval import result_url, subject_url

PROVIDER_CODE = "socialstyrelsen"
SOURCE = "Socialstyrelsen"
"""Attribution requested by the SDB API documentation."""

MAPPING_VERSION = 1
"""Increment when a mapping change must be distinguishable in accepted state."""

TIME_DIMENSIONS = ("ar", "manad")
GEO_DIMENSIONS = ("region",)
METRIC_DIMENSIONS = ("matt",)
_OMITTED_CATEGORY_FIELDS = {"id", "info"}
_YEAR = re.compile(r"\d{4}")


@dataclass(frozen=True)
class Subject:
    """An SDB subject (``ämne``), which becomes one Dataset.

    Attributes:
        code: SDB subject name, used as the dataset code.
        label: Subject text in the document language.
        time_unit: Statistical period granularity, if known.
    """

    code: str
    label: str
    time_unit: str | None


def time_unit_from_swedish_label(label: str) -> str | None:
    """Infer the period granularity from a Swedish subject label suffix."""
    normalized = label.strip().lower()
    if normalized.endswith("årsuppgifter"):
        return "annual"
    if normalized.endswith("månadsuppgifter"):
        return "monthly"
    return None


class _TextExtractor(HTMLParser):
    _BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "center"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append("\n" if tag in self._BREAKS else " ")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append("\n" if tag in self._BREAKS else " ")

    def handle_data(self, data: str) -> None:
        self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str | None) -> str | None:
    """Convert SDB ``info`` HTML to plain text lines, or None when empty."""
    if not html or not html.strip():
        return None
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (
        re.sub(r"\s+", " ", line).strip()
        for line in "".join(extractor.parts).split("\n")
    )
    text = "\n".join(line for line in lines if line)
    return text or None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _category_label(category: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return a category's label and the SDB field it came from."""
    if "drgkod" in category:
        field = "drgtext" if _clean(category.get("drgkod")) else "mdctext"
    elif "sjukhustext" in category:
        field = "sjukhustext" if _clean(category.get("sjukhustext")) else "lantext"
    else:
        field = "text"
    label = _clean(category.get(field))
    return label, field if label else None


def _is_root_marker(grupp: object) -> bool:
    """SDB marks top-level categories with ``grupp`` values such as "00" or "0"."""
    return not (_clean(grupp) or "").strip("0")


def _parent_code(
    category: Mapping[str, Any],
    categories: Sequence[Mapping[str, Any]],
    codes: set[str],
) -> str | None:
    code = str(category["id"])
    if "grupp" in category:
        # Only classification variables (with ``typ``) use ``grupp`` as a parent id;
        # elsewhere, such as tandhälsa indicators, it is a group number.
        if "typ" not in category:
            return None
        parent = _clean(category.get("grupp"))
        return parent if parent in codes and parent != code else None
    if "drgkod" in category and _clean(category.get("drgkod")):
        return _first_code(
            categories,
            lambda other: (
                other.get("mdckod") == category.get("mdckod")
                and not _clean(other.get("drgkod"))
            ),
        )
    if "sjukhustext" in category and _clean(category.get("sjukhustext")):
        return _first_code(
            categories,
            lambda other: (
                other.get("lantext") == category.get("lantext")
                and not _clean(other.get("sjukhustext"))
            ),
        )
    return None


def _first_code(categories: Sequence[Mapping[str, Any]], predicate) -> str | None:
    for other in categories:
        if predicate(other):
            return str(other["id"])
    return None


def build_dimension(
    variable: Mapping[str, Any], categories: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build a JSON-stat2 dimension from an SDB variable and its categories."""
    codes = [str(category["id"]) for category in categories]
    known = set(codes)
    labels: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    extras: dict[str, dict[str, Any]] = {}

    for category in categories:
        code = str(category["id"])
        label, label_field = _category_label(category)
        if label:
            labels[code] = label
        if note := html_to_text(category.get("info")):
            notes[code] = [note]
        parent = _parent_code(category, categories, known)
        if parent is not None:
            children.setdefault(parent, []).append(code)
        omitted = _OMITTED_CATEGORY_FIELDS | {label_field}
        if parent is not None or _is_root_marker(category.get("grupp")):
            omitted.add("grupp")
        extra = {
            key: value.strip() if isinstance(value, str) else value
            for key, value in category.items()
            if key not in omitted and _clean(value) is not None
        }
        if extra:
            extras[code] = extra

    category_object: dict[str, Any] = {"index": codes}
    if labels:
        category_object["label"] = labels
    if notes:
        category_object["note"] = notes
    if children:
        category_object["child"] = children

    dimension: dict[str, Any] = {}
    if label := _clean(variable.get("text")):
        dimension["label"] = label
    dimension["category"] = category_object
    if note := html_to_text(variable.get("info")):
        dimension["note"] = [note]
    if extras:
        dimension["extension"] = {PROVIDER_CODE: {"categories": extras}}
    return dimension


def build_dataset(
    subject: Subject,
    language: str,
    variables: Sequence[Mapping[str, Any]],
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    result_dimensions: Mapping[str, str] | None = None,
    added_periods: Sequence[str] = (),
    source_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a metadata-only Dataset document for one subject and language.

    Args:
        subject: The subject in ``language``.
        language: Document language, ``sv`` or ``en``.
        variables: SDB variables in API order.
        categories: Categories in API order, keyed by variable name.
        result_dimensions: Variables that SDB's result endpoint names differently,
            recorded for retrieval under ``extension.socialstyrelsen``.
        added_periods: ``ar`` codes with observations that the listing lacks; they
            are added without labels and marked ``from_results``.
        source_record: The subject's listing row, kept as accepted source evidence.
    """
    if added_periods:
        categories = {
            **categories,
            "ar": sorted(
                [
                    *categories["ar"],
                    *({"id": period, "from_results": True} for period in added_periods),
                ],
                key=lambda category: int(category["id"]),
            ),
        }
    dimension_ids = [str(variable["namn"]) for variable in variables]
    dimensions = {
        str(variable["namn"]): build_dimension(
            variable, categories[str(variable["namn"])]
        )
        for variable in variables
    }
    role = {
        name: present
        for name, candidates in (
            ("time", TIME_DIMENSIONS),
            ("geo", GEO_DIMENSIONS),
            ("metric", METRIC_DIMENSIONS),
        )
        if (present := [code for code in candidates if code in dimensions])
    }
    nordicintel: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": subject.code,
        "language": language,
        "metadata_url": subject_url(language, subject.code),
        "data_url": result_url(language, subject.code),
    }
    time_unit = subject.time_unit
    if time_unit == "annual" and "ar" in dimensions:
        periods = sorted(dimensions["ar"]["category"]["index"])
        if not all(_YEAR.fullmatch(period) for period in periods):
            time_unit = "other"  # e.g. three-year windows such as "1987-1989"
        nordicintel["first_period"] = periods[0]
        nordicintel["last_period"] = periods[-1]
    if time_unit:
        nordicintel["time_unit"] = time_unit

    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": subject.label,
        "source": SOURCE,
        "id": dimension_ids,
        "size": [len(dimensions[code]["category"]["index"]) for code in dimension_ids],
    }
    if role:
        document["role"] = role
    document["dimension"] = dimensions
    document["value"] = []
    document["extension"] = {"nordicintel": nordicintel}
    provider: dict[str, Any] = {}
    if result_dimensions:
        provider["result_dimensions"] = dict(result_dimensions)
    if source_record is not None:
        provider["source_record"] = dict(source_record)
    if provider:
        document["extension"][PROVIDER_CODE] = provider
    return document

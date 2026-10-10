"""Map Trafikanalys API combinations to metadata-only NordicIntel Dataset documents."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from apps.harvest.metadata_adapters.trafa.api import (
    DATA_URL,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    documentation_url,
    structure_url,
)
from apps.harvest.metadata_adapters.trafa.source import (
    REGION,
    TIME,
    Combination,
    Product,
    Variable,
)

SOURCE = {"sv": "Trafikanalys", "en": "Transport Analysis"}
"""Citation requested by Trafikanalys: "Källa: Trafikanalys" / "Source: Transport
Analysis" (https://www.trafa.se/om-oss/om-webbplatsen/)."""

CONTACT = {
    "organization": "Trafikanalys",
    "email": "trafikanalys@trafa.se",
    "phone": "010-414 42 00",
    "address": "Rosenlundsgatan 54, 118 63 Stockholm",
    "url": "https://www.trafa.se/",
}
MEASURE_LABEL = {"sv": "Mått", "en": "Measure"}
"""Adapter label for the synthetic measure dimension."""

STATUS_NOTE = {
    "sv": (
        'status återger Trafikanalys markering (t.ex. "-" eller "..") för celler '
        "som har en markering i stället för ett värde, samt texten i Trafikanalys "
        'cellnoter (t.ex. "Reviderad uppgift"), åtskilda med "; ". '
        "Trafikanalys API-dokumentation anger inte vad markeringarna betyder."
    ),
    "en": (
        'status gives the Trafikanalys marker (such as "-" or "..") for cells '
        "that carry a marker instead of a value, and the text of Trafikanalys cell "
        'notes (such as "Reviderad uppgift", revised figure), separated by "; ". '
        "The Trafikanalys API documentation does not define the markers."
    ),
}
MAPPING_VERSION = 1
"""Increment when a mapping change must be distinguishable in accepted state."""

TOTAL = "t1"
"""Category code of Trafikanalys' "Totalt" (total) values."""
TIME_UNITS = {"ar": "annual", "kvartal": "quarterly", "manad": "monthly"}
_TIME_UNIT_ORDER = ("other", "monthly", "quarterly", "annual")
_JOIN = {"sv": ("efter", "och"), "en": ("by", "and")}
_YEAR = re.compile(r"\d{4}")
_UNKNOWN_DATE = "0001-01-01"


@dataclass(frozen=True)
class MeasureProbe:
    """What a small ``/data`` request reveals about a set of measures.

    Attributes:
        units: Measure name to its non-empty ``Unit`` text.
        next_publish: ``NextPublishDate`` unless it is Trafikanalys' unknown default.
    """

    units: dict[str, str] = field(default_factory=dict)
    next_publish: str | None = None


def probe_query(product: Product, combination: Combination) -> str:
    """Return a one-period ``/data`` query for the combination's measures.

    It names the first time variable with its latest category only, so the response
    stays a single row while its header describes every measure.
    """
    names = [product.name, *combination.measures]
    for variable in _variables(product, combination):
        if variable.data_type == TIME and variable.categories:
            names.append(f"{variable.name}:{max(c.code for c in variable.categories)}")
            break
    return "|".join(names)


def parse_probe(response: Mapping[str, Any]) -> MeasureProbe:
    """Extract units and the next publication date from a ``/data`` response.

    Raises:
        ValueError: Trafikanalys reported errors for the query.
    """
    if errors := response.get("Errors"):
        raise ValueError(f"Trafikanalys reported errors: {errors}")
    units = {}
    for column in response.get("Header", {}).get("Column", []):
        if column.get("Type") == "M" and (
            unit := str(column.get("Unit") or "").strip()
        ):
            units[str(column["Name"])] = unit
    next_publish = str(response.get("NextPublishDate") or "").strip()
    if not next_publish or next_publish.startswith(_UNKNOWN_DATE):
        next_publish = None
    return MeasureProbe(units, next_publish)


def _variables(product: Product, combination: Combination) -> list[Variable]:
    """The combination's variables in product structure order."""
    return [
        variable
        for name, variable in product.variables.items()
        if name in combination.variables
    ]


def _translated(text: str, reference: str | None, *codes: str) -> str | None:
    """Return ``text`` if it may be shown in the document language, else None.

    Without a reference (Swedish documents) every non-empty text is kept. With the
    Swedish reference (English documents) text is kept only when it is a real
    translation: different from the Swedish text and from the item's API codes,
    which Trafikanalys echoes when no translation exists.
    """
    if not text:
        return None
    if reference is None:
        return text
    if text == reference or text.casefold() in {code.casefold() for code in codes}:
        return None
    return text


def untranslated_items(
    combination: Combination, product: Product, reference: Product
) -> list[str]:
    """Return items whose English label is missing or not a real translation.

    An English document is emitted only when this is empty: the product and every
    measure and variable of the combination need real English labels.
    """
    missing = []
    if not _translated(product.label, reference.label, product.name, product.unique_id):
        missing.append(product.unique_id)
    for name in (*combination.measures, *combination.variables):
        item = product.measures.get(name) or product.variables.get(name)
        original = reference.measures.get(name) or reference.variables.get(name)
        if (
            item is None
            or original is None
            or not _translated(item.label, original.label, name)
        ):
            missing.append(name)
    return missing


def _join(labels: list[str], language: str) -> str:
    conjunction = _JOIN[language][1]
    if len(labels) < 2:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} {conjunction} {labels[-1]}"


def dataset_label(combination: Combination, product: Product, language: str) -> str:
    """Build a label such as "Bussar: Antal i trafik efter år och bussklass"."""
    measures = [
        product.measures[name].label or name
        for name in product.measures
        if name in combination.measures
    ]
    variables = [
        (variable.label or variable.name).lower()
        if language == "sv"
        else variable.label or variable.name
        for variable in _variables(product, combination)
    ]
    label = f"{product.label}: {', '.join(measures)}"
    if variables:
        label += f" {_JOIN[language][0]} {_join(variables, language)}"
    return label


def _variable_dimension(
    variable: Variable, reference: Variable | None
) -> dict[str, Any]:
    english = reference is not None
    originals = {c.code: c for c in reference.categories} if reference else {}
    labels: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    for category in variable.categories:
        original = originals.get(category.code)
        if english and original is None:
            continue
        if label := _translated(
            category.label, original.label if original else None, category.code
        ):
            labels[category.code] = label
        if note := _translated(
            category.description, original.description if original else None
        ):
            notes[category.code] = [note]
    category_object: dict[str, Any] = {"index": [c.code for c in variable.categories]}
    if labels:
        category_object["label"] = labels
    if notes:
        category_object["note"] = notes

    dimension: dict[str, Any] = {}
    if label := _translated(
        variable.label, reference.label if reference else None, variable.name
    ):
        dimension["label"] = label
    dimension["category"] = category_object
    if note := _translated(
        variable.description, reference.description if reference else None
    ):
        dimension["note"] = [note]
    trafa: dict[str, Any] = {}
    if variable.hierarchy is not None:
        hierarchy: dict[str, str] = {"name": variable.hierarchy.name}
        original = reference.hierarchy if reference else None
        if not (english and original is None) and (
            label := _translated(
                variable.hierarchy.label,
                original.label if original else None,
                variable.hierarchy.name,
            )
        ):
            hierarchy["label"] = label
        trafa["hierarchy"] = hierarchy
    if variable.parent is not None:
        trafa["parent"] = variable.parent
    # Omitting a variable is unreliable (queries outside the listed combinations
    # can return no rows), so elimination selects Trafikanalys' "t1" total.
    total = variable.data_type != TIME and any(
        category.code == TOTAL for category in variable.categories
    )
    dimension["extension"] = {
        "nordicintel": (
            {"elimination": True, "elimination_value": TOTAL}
            if total
            else {"elimination": False}
        )
    }
    if trafa:
        dimension["extension"][PROVIDER_CODE] = trafa
    return dimension


def _measure_dimension(
    combination: Combination,
    product: Product,
    language: str,
    probe: MeasureProbe,
    reference: Product | None,
    reference_probe: MeasureProbe | None,
) -> dict[str, Any]:
    codes = [name for name in product.measures if name in combination.measures]
    labels: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    units: dict[str, dict[str, str]] = {}
    for code in codes:
        measure = product.measures[code]
        original = reference.measures[code] if reference else None
        if label := _translated(
            measure.label, original.label if original else None, code
        ):
            labels[code] = label
        if note := _translated(
            measure.description, original.description if original else None
        ):
            notes[code] = [note]
        if unit := _translated(
            probe.units.get(code, ""),
            reference_probe.units.get(code, "") if reference_probe else None,
        ):
            units[code] = {"label": unit}
    category: dict[str, Any] = {"index": codes}
    if labels:
        category["label"] = labels
    if notes:
        category["note"] = notes
    if units:
        category["unit"] = units
    return {
        "label": MEASURE_LABEL[language],
        "category": category,
        "extension": {"nordicintel": {"elimination": False}},
    }


def _time_unit(variables: list[Variable]) -> str | None:
    units = [
        TIME_UNITS.get(variable.name, "other")
        for variable in variables
        if variable.data_type == TIME
    ]
    if not units:
        return None
    return min(units, key=_TIME_UNIT_ORDER.index)


def build_dataset(
    combination: Combination,
    product: Product,
    language: str,
    probe: MeasureProbe,
    *,
    reference: Product | None = None,
    reference_probe: MeasureProbe | None = None,
) -> dict[str, Any]:
    """Build the metadata-only Dataset document for one combination and language.

    Args:
        combination: The API combination behind the dataset.
        product: The product parsed in ``language``.
        language: ``sv`` or ``en``.
        probe: Units and next publication date from a ``/data`` request in
            ``language``.
        reference: For English, the Swedish product; English text equal to the
            Swedish text or to API codes is left out.
        reference_probe: For English, the Swedish probe; units are compared likewise.
    """
    if MEASURE_DIMENSION in product.variables:
        raise ValueError(f"variable name {MEASURE_DIMENSION!r} clashes with measures")
    variables = _variables(product, combination)
    if reference is not None and (
        missing := untranslated_items(combination, product, reference)
    ):
        raise ValueError(f"no English translation for {missing}")
    dimensions: dict[str, Any] = {
        variable.name: _variable_dimension(
            variable, reference.variables.get(variable.name) if reference else None
        )
        for variable in variables
    }
    dimensions[MEASURE_DIMENSION] = _measure_dimension(
        combination, product, language, probe, reference, reference_probe
    )
    ids = list(dimensions)
    role: dict[str, list[str]] = {}
    if time := [v.name for v in variables if v.data_type == TIME]:
        role["time"] = time
    if geo := [v.name for v in variables if v.data_type == REGION]:
        role["geo"] = geo
    role["metric"] = [MEASURE_DIMENSION]

    nordicintel: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": combination.dataset_code,
        "language": language,
        "doc_url": documentation_url(product.unique_id),
        "metadata_url": structure_url(product.name, language),
        "data_url": DATA_URL,
    }
    if time_unit := _time_unit(variables):
        nordicintel["time_unit"] = time_unit
    if time == ["ar"]:
        periods = dimensions["ar"]["category"]["index"]
        if all(_YEAR.fullmatch(period) for period in periods):
            nordicintel["first_period"] = min(periods)
            nordicintel["last_period"] = max(periods)
    if probe.next_publish:
        nordicintel["next_release"] = probe.next_publish
    nordicintel["contacts"] = [CONTACT]

    trafa: dict[str, Any] = {
        "product": product.name,
        "product_id": product.unique_id,
        "measure_dimension": MEASURE_DIMENSION,
        "api_combinations": list(combination.listed),
    }
    if product.active_from:
        trafa["active_from"] = product.active_from
    trafa["source_record"] = {
        "product": {
            "name": product.name,
            "unique_id": product.unique_id,
            "label": product.label,
        },
        "combination": {
            "measures": list(combination.measures),
            "variables": list(combination.variables),
        },
    }

    notes = []
    if description := _translated(
        product.description, reference.description if reference else None
    ):
        notes.append(description)
    notes.append(STATUS_NOTE[language])

    return {
        "version": "2.0",
        "class": "dataset",
        "label": dataset_label(combination, product, language),
        "source": SOURCE[language],
        "note": notes,
        "id": ids,
        "size": [len(dimensions[code]["category"]["index"]) for code in ids],
        "role": role,
        "dimension": dimensions,
        "value": [],
        "extension": {"nordicintel": nordicintel, PROVIDER_CODE: trafa},
    }

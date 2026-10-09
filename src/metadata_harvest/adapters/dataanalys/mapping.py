"""Map one analysverktyg indicator to a metadata-only NordicIntel Dataset document."""

import re
from datetime import UTC, datetime
from typing import Any

from metadata_harvest.adapters.dataanalys.api import (
    AGE,
    DATA_URL,
    LEVELS,
    MEASURE,
    PROVIDER_CODE,
    REGION,
    SEX,
    TIME,
    dashboard_datasets_url,
    dashboard_url,
)
from metadata_harvest.adapters.dataanalys.source import (
    AGE_TOTAL,
    SEX_TOTAL,
    Area,
    Cube,
    Link,
    parse_anchor,
)

MAPPING_VERSION = 1
"""Increment when a mapping change must be distinguishable in accepted state."""

LABELS = {
    REGION: "Region",
    SEX: "Kön",
    AGE: "Ålder",
    TIME: "Mätperiod",
    MEASURE: "Mått",
}
"""Adapter labels for the dimensions (the source tables name only columns)."""
TIME_UNITS = {"1_ar": "annual"}
STATUS_NOTE = (
    "status återger Socialstyrelsens kommentar till cellen (kolumnen {column}) "
    'ordagrant, både för celler utan värde (t.ex. "Uppgift saknas (via vården i '
    'Siffror)"), som har värdet null, och för värden med en anmärkning (t.ex. '
    '"Exkl. Uppsala"). Analysverktyget förklarar inte kommentarerna närmare.'
)
"""Explains ``status`` in retrieval results; added when the values table has a
comment column."""


def _text(row: dict[str, Any], name: str) -> str | None:
    value = row.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def indicator_label(code: str, row: dict[str, Any]) -> str:
    """``MTypeName``, else ``MName``, else the catalogue link text without its trailing
    ``". {Unit}"``, else the code."""
    for name in ("MTypeName", "MName"):
        if text := _text(row, name):
            return text
    text = parse_anchor(row.get("HrefIndicatorDashboard"))[1]
    if text and (unit := _text(row, "Unit")) and text.endswith(f". {unit}"):
        text = text[: -len(f". {unit}")].strip()
    return text or code


def _date(milliseconds: Any) -> str | None:
    if not isinstance(milliseconds, (int, float)) or isinstance(milliseconds, bool):
        return None
    try:
        moment = datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.date().isoformat() if 1900 <= moment.year <= 2099 else None


def _sentence(prefix: str, text: str | None) -> str | None:
    if not text:
        return None
    return f"{prefix}: {text}" + ("" if text.endswith((".", "!", "?")) else ".")


def _absolute(url: str | None) -> str | None:
    return url if url and re.match(r"^https?://\S+$", url, re.IGNORECASE) else None


def _dimension(
    codes: list[str], label: str, total: str | None, extension: dict | None = None
) -> dict[str, Any]:
    eliminable = total is not None and total in codes
    dimension: dict[str, Any] = {
        "label": label,
        "category": {"index": codes, "label": {code: code for code in codes}},
        "extension": {
            "nordicintel": (
                {"elimination": True, "elimination_value": total}
                if eliminable
                else {"elimination": False}
            )
        },
    }
    if extension:
        dimension["extension"][PROVIDER_CODE] = extension
    return dimension


def build_dataset(
    area: Area,
    code: str,
    row: dict[str, Any],
    cube: Cube,
    columns: dict[str, str],
    link: Link,
    *,
    subject_label: str,
) -> dict[str, Any]:
    """Build the Swedish metadata-only document of one catalogue row.

    Args:
        area: The indicator's area and its two tables.
        code: ``MBusinessKey`` (= ``Matt_id`` in the values table).
        row: The catalogue row, kept verbatim as ``source_record``.
        cube: Distinct dimension categories from the values table.
        columns: Logical ``Trender`` column name to source column.
        link: The resolved web page.
        subject_label: The area's name.
    """
    label = indicator_label(code, row)
    riket = [c for c, level in cube.regions if level == LEVELS[0]]
    dimensions: dict[str, Any] = {
        REGION: _dimension(
            [c for c, _ in cube.regions],
            LABELS[REGION],
            riket[0] if len(riket) == 1 else None,
            {"categories": {c: {"level": level} for c, level in cube.regions}},
        )
    }
    column_map: dict[str, Any] = {
        REGION: {
            "level": columns["level"],
            "code": columns["code"],
            "levels": list(LEVELS),
        }
    }
    if cube.sexes:
        dimensions[SEX] = _dimension(list(cube.sexes), LABELS[SEX], SEX_TOTAL)
        column_map[SEX] = columns["sex"]
    if cube.ages:
        dimensions[AGE] = _dimension(list(cube.ages), LABELS[AGE], AGE_TOTAL)
        column_map[AGE] = columns["age"]
    dimensions[TIME] = _dimension(list(cube.periods), LABELS[TIME], None)
    column_map[TIME] = columns["period"]
    category: dict[str, Any] = {"index": [code], "label": {code: label}}
    if unit := _text(row, "Unit"):
        category["unit"] = {code: {"label": unit}}
    dimensions[MEASURE] = {
        "label": LABELS[MEASURE],
        "category": category,
        "extension": {"nordicintel": {"elimination": False}},
    }
    column_map[MEASURE] = columns["indicator"]
    column_map["value"] = columns["value"]
    if "status" in columns:
        column_map["status"] = columns["status"]
    ids = list(dimensions)

    nordicintel: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": f"{area.code}|{code}",
        "language": "sv",
    }
    if link.dashboard is not None:
        nordicintel["source_url"] = dashboard_url(link.dashboard.slug)
    if doc_url := _absolute(_text(row, "LinkIndicatorDescription")):
        nordicintel["doc_url"] = doc_url
    if link.dashboard is not None:
        nordicintel["metadata_url"] = dashboard_datasets_url(link.dashboard.id)
    nordicintel["data_url"] = DATA_URL
    if description := _text(row, "Description"):
        nordicintel["description"] = description
    if cube.period_types:
        units = {TIME_UNITS.get(kind, "other") for kind in cube.period_types}
        nordicintel["time_unit"] = units.pop() if len(units) == 1 else "other"
    nordicintel["first_period"] = cube.periods[0]
    nordicintel["last_period"] = cube.periods[-1]
    nordicintel["subject"] = {"code": area.code, "label": subject_label}

    native: dict[str, Any] = {
        "area": area.code,
        "indicator": code,
        "values_dataset_id": area.values_id,
        "values_dataset": area.values_dataset,
        "catalogue_dataset_id": area.catalogue_id,
        "catalogue_dataset": area.catalogue_dataset,
        "columns": column_map,
        "link_resolution": link.resolution,
    }
    if link.dashboard is not None:
        native["dashboard_id"] = link.dashboard.id
        native["dashboard_slug"] = link.dashboard.slug
    if cube.period_types:
        native["measure_period_type"] = (
            cube.period_types[0]
            if len(cube.period_types) == 1
            else list(cube.period_types)
        )
    if row.get("DataGenerationDate") is not None:
        native["updated_original"] = row["DataGenerationDate"]
    native["source_record"] = row

    notes = [
        note
        for note in (
            _sentence("Önskat värde", _text(row, "DesiredValue")),
            _sentence("Indikatortyp", _text(row, "InputType")),
            STATUS_NOTE.format(column=columns["status"])
            if "status" in columns
            else None,
        )
        if note
    ]
    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": label,
    }
    if source := _text(row, "DataSource"):
        document["source"] = source
    if updated := _date(row.get("DataGenerationDate")):
        document["updated"] = updated
    if notes:
        document["note"] = notes
    document.update(
        {
            "id": ids,
            "size": [len(dimensions[name]["category"]["index"]) for name in ids],
            "role": {"geo": [REGION], "time": [TIME], "metric": [MEASURE]},
            "dimension": dimensions,
            "value": [],
            "extension": {"nordicintel": nordicintel, PROVIDER_CODE: native},
        }
    )
    return document

"""Map one Försäkringskassan table to a metadata-only NordicIntel Dataset document."""

import logging
import re
from collections.abc import Mapping
from typing import Any

from apps.harvest.metadata_adapters.forsakringskassan.client import (
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    meta_url,
    source_url,
    table_url,
)
from apps.harvest.metadata_adapters.forsakringskassan.source import (
    MEASURES_HEADING,
    Dimension,
    Listed,
    Report,
    Table,
    parse_about,
    plain,
    stated_unit,
)

logger = logging.getLogger("forsakringskassan_logger")

MAPPING_VERSION = 1
"""Increment when a mapping change must rebuild every report in incremental runs."""

SOURCE = "Försäkringskassan"
LANGUAGE = "sv"
TOTAL = "ALL"
"""Category code of the totals (``Kvinnor och män``, ``Riket``, ``Samtliga …``)."""
MEASURE_LABEL = "Mått"
DISCONTINUED = "(uppdateras ej)"
"""Report names marked "not updated" (six reports in October 2026)."""

ROLE_ALIASES: dict[str, tuple[str | None, str]] = {
    # key: (shared role, concept); dimension ids are never renamed.
    "ar": ("time", "year"),
    "manad": ("time", "month"),
    "man": ("time", "month"),
    "vecka": ("time", "week"),
    "tidsperiod": ("time", "period"),
    "lan": ("geo", "county"),
    "lan_kod": ("geo", "county"),
    "lan_kod1": ("geo", "county"),
    "lan_kod2": ("geo", "county"),
    "lan_kod3": ("geo", "county"),
    "lan_kod4": ("geo", "county"),
    "lan_mottagning": ("geo", "county"),
    "kon": (None, "sex"),
    "kon_kod": (None, "sex"),
    "kon_kod1": (None, "sex"),
    "kon_kod2": (None, "sex"),
    "kon_kod3": (None, "sex"),
}
"""Dimension keys that name the same concept across reports. ``ar`` labelled as a
birth year ("Barnets födelseår") is not given the time role."""
_TIME_UNITS = {
    "year": "annual",
    "month": "monthly",
    "week": "weekly",
    "period": "other",
}
_TIME_UNIT_ORDER = ("other", "weekly", "monthly", "annual")
_YEAR = re.compile(r"\d{4}")

STATUS_NOTE = (
    'status ".." markerar celler som Försäkringskassan inte redovisar för att skydda '
    "enskilda individer (rojd i API:et). Försäkringskassans text: "
    '"När vi inte kan redovisa uppgifter markeras celler i tabeller med ".."." '
    "Celler som saknar värde utan denna markering redovisas inte."
)


def role_of(dimension: Dimension) -> tuple[str | None, str | None]:
    """The shared role and concept of a dimension key, from :data:`ROLE_ALIASES`."""
    role, concept = ROLE_ALIASES.get(dimension.key, (None, None))
    if role == "time" and "födelse" in dimension.label.casefold():
        return None, None
    return role, concept


def _dimension(dimension: Dimension) -> dict[str, Any]:
    codes = [category.code for category in dimension.categories]
    category: dict[str, Any] = {"index": codes}
    if labels := {c.code: c.label for c in dimension.categories if c.label}:
        category["label"] = labels
    node: dict[str, Any] = {}
    if dimension.label:
        node["label"] = dimension.label
    node["category"] = category
    node["extension"] = {
        "nordicintel": (
            {"elimination": True, "elimination_value": TOTAL}
            if TOTAL in codes
            else {"elimination": False}
        )
    }
    native: dict[str, Any] = {}
    if dimension.group:
        native["group"] = dimension.group
    if concept := role_of(dimension)[1]:
        native["concept"] = concept
    if native:
        node["extension"][PROVIDER_CODE] = native
    return node


def _measure_dimension(report: Report, about) -> dict[str, Any]:
    described = {
        heading.casefold(): text
        for heading, text in about.sections.get(MEASURES_HEADING, {}).items()
        if heading
    }
    labels, units, notes = {}, {}, {}
    for measure in report.measures:
        if measure.label:
            labels[measure.key] = measure.label
        text = described.get(measure.label.casefold()) if measure.label else None
        if unit := stated_unit(measure.label):
            units[measure.key] = {"label": unit}
        if text:
            notes[measure.key] = [text]
    category: dict[str, Any] = {"index": [m.key for m in report.measures]}
    if labels:
        category["label"] = labels
    if notes:
        category["note"] = notes
    if units:
        category["unit"] = units
    return {
        "label": MEASURE_LABEL,
        "category": category,
        "extension": {"nordicintel": {"elimination": False}},
    }


def _time_unit(concepts: list[str]) -> str | None:
    units = [_TIME_UNITS[concept] for concept in concepts if concept in _TIME_UNITS]
    return min(units, key=_TIME_UNIT_ORDER.index) if units else None


def build_dataset(
    report: Report,
    table: Table,
    listed: Listed,
    texts: Mapping[str, Any],
    *,
    meta_sha256: str,
    texts_sha256: str,
) -> dict[str, Any]:
    """Build the metadata-only Dataset document of one table.

    Raises:
        ValueError: The texts have no name for the report (no label-less documents
            are published), or a dimension clashes with the measure dimension.
    """
    entry = texts.get(f"MEKA.report.{report.key}")
    name = plain(entry.get("name")) if isinstance(entry, dict) else ""
    if not name:
        raise ValueError(f"texts have no name for report {report.key!r}")
    if MEASURE_DIMENSION in table.dimensions:
        raise ValueError(f"dimension {MEASURE_DIMENSION!r} clashes with measures")
    forman = texts.get(f"MEKA.forman.{report.forman}")
    area = texts.get(f"MEKA.area.{report.area}")
    forman_label = plain(forman.get("rubrik")) if isinstance(forman, dict) else ""
    area_label = plain(area.get("rubrik")) if isinstance(area, dict) else ""
    about = parse_about(entry.get("about"))

    dimensions: dict[str, Any] = {
        key: _dimension(report.dimensions[key]) for key in table.dimensions
    }
    dimensions[MEASURE_DIMENSION] = _measure_dimension(report, about)
    ids = list(dimensions)
    role: dict[str, list[str]] = {}
    concepts = []
    for key in table.dimensions:
        shared, concept = role_of(report.dimensions[key])
        if shared:
            role.setdefault(shared, []).append(key)
            concepts.append(concept)
    role["metric"] = [MEASURE_DIMENSION]

    label = name if len(report.tables) == 1 else f"{name}: {table.label or table.name}"
    nordicintel: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": f"{report.key}/{table.name}",
        "language": LANGUAGE,
    }
    if description := plain(entry.get("about")):
        nordicintel["description"] = description
    if DISCONTINUED in name:
        logger.debug("Report %s is marked discontinued in its name", report.key)
        nordicintel["discontinued"] = True
    if report.official is not None:
        nordicintel["official_statistics"] = report.official
    if time_unit := _time_unit(concepts):
        nordicintel["time_unit"] = time_unit
    if role.get("time") == ["ar"]:
        years = dimensions["ar"]["category"]["index"]
        if all(_YEAR.fullmatch(year) for year in years):
            nordicintel["first_period"] = min(years)
            nordicintel["last_period"] = max(years)
    subject = {"code": report.forman}
    if forman_label:
        subject["label"] = forman_label
    nordicintel["subject"] = subject
    if area_label and forman_label:
        nordicintel["paths"] = [
            [
                {"id": report.area, "label": area_label},
                {"id": report.forman, "label": forman_label},
            ]
        ]
    nordicintel["source_url"] = source_url(report.area, report.key)
    nordicintel["metadata_url"] = meta_url(report.key)
    nordicintel["data_url"] = table_url(report.key, table.name)
    if quality := [
        url
        for url, text in about.links
        if "kvalitetsdeklaration" in (url + text).lower()
    ]:
        nordicintel["doc_url"] = quality[0]
    if about.emails:
        nordicintel["contacts"] = [
            {"organization": SOURCE, "email": email} for email in about.emails
        ]

    native: dict[str, Any] = {
        "report": report.key,
        "table": table.name,
        "breakdown": {"key": table.key, "label": table.label},
        "area": report.area,
        "forman": report.forman,
        "measure_dimension": MEASURE_DIMENSION,
        "meta_version": report.version,
        "meta_revision": report.revision,
    }
    if report.computed:
        native["computed"] = list(report.computed)
    native["source_record"] = {
        "catalogue": listed.record(),
        "meta": {
            "key": report.key,
            "area": report.area,
            "forman": report.forman,
            "official": report.official,
            "version": report.version,
            "revision": report.revision,
            "tables": [
                {"key": t.key, "label": t.label, "table": t.name} for t in report.tables
            ],
        },
        "meta_sha256": meta_sha256,
        "texts_sha256": texts_sha256,
    }

    notes = []
    if ingress := plain(entry.get("ingress")):
        notes.append(ingress)
    notes.append(STATUS_NOTE)

    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": label,
        "source": SOURCE,
        "note": notes,
        "id": ids,
        "size": [len(dimensions[key]["category"]["index"]) for key in ids],
        "role": role,
        "dimension": dimensions,
        "value": [],
        "extension": {"nordicintel": nordicintel, PROVIDER_CODE: native},
    }
    logger.debug(
        "Built dataset %s/%s: %d dimensions, %d describedby links, %d contacts",
        report.key,
        table.name,
        len(ids),
        len(about.links),
        len(about.emails),
    )
    if about.links:
        document["link"] = {
            "describedby": [
                {"href": url, "label": text, "type": "application/pdf"}
                for url, text in about.links
            ]
        }
    return document

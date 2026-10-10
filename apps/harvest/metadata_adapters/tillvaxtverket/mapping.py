"""Map Tillväxtverket ``.cbase`` files to metadata-only NordicIntel Dataset documents.

A document is built in two steps. :func:`build_cube` turns the flat rows'
distinct values into dimensions (only when the change signal moved);
:func:`apply_dcat` adds the data.europa.eu title, description and contact, and is
re-applied to the accepted cube on every run because those records are fetched
every run.
"""

import copy
import logging
import re
from typing import Any

from apps.harvest.metadata_adapters.tillvaxtverket.api import (
    API_DOCUMENTATION_URL,
    MEASURE_DIMENSION,
    PROVIDER_CODE,
    REGION_DIMENSION,
    data_url,
    dcat_page_url,
    labels_url,
    metadata_url,
)
from apps.harvest.metadata_adapters.tillvaxtverket.source import (
    COUNTRY_COLUMN,
    COUNTY_NAME,
    LOAD_DATE,
    MUNICIPALITY_NAME,
    Category,
    Probe,
    Region,
    Spec,
)
from retrieval.data_adapters.tillvaxtverket import REGION_COLUMNS, SUPPRESSED

MAPPING_VERSION = 2
"""Increment when a mapping change must rebuild every accepted document."""

SOURCE = "Tillväxtverket"
"""Fallback attribution when the data.europa.eu record names no publisher."""
TIME_DIMENSION = "Tid"
DIMENSION_LABELS = {
    REGION_DIMENSION: "Region",
    "Land": "Land",
    MEASURE_DIMENSION: "Tabellinnehåll",
}
"""Adapter labels for dimensions built from several columns."""
OFFICIAL = "sveriges officiella statistik"
_PLACEHOLDER_EMAIL = re.compile(r"^fornamn\.efternamn@", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=\.)\s+")

REGION_NOTE = (
    "Regionkoderna bildas av adaptern ur källkolumnerna NIVA_NAMN, LAN_KOD och "
    "KOMMUN_KOD: riket, lan:<länskod> och kommun:<kommunkod>. "
    "lan:Sekretesskyddad bevarar källans kategori på länsnivå där länskoden "
    'och länsnamnet är "Sekretesskyddad"; den anger inget identifierat län. '
    "kommun:<länskod>:Sekretesskyddad avser källans kommunrader i länet där "
    'kommunkoden är "Sekretesskyddad". I riksraderna innehåller läns- och '
    'kommunkolumnerna bara utfyllnad ("Sekretesskyddad" eller "Riket"), som inte '
    "bildar någon kategori."
)
TYPE_NOTE = (
    'Kategorin Sekretesskyddad är källans rader där anläggningstypen anges som "'
    'Sekretesskyddad".'
)
COUNTRY_NOTE = (
    "Kategorikoder är källans LAND_KOD och etiketter dess LAND_NAMN. Landgrupperna "
    "(LANDGRP_*) finns per kategori under extension.tillvaxtverket.categories."
)
MEASURE_NOTE = (
    "Varje kategori är en måttkolumn i källan. Etiketterna kommer från "
    "Tillväxtverkets variabeletiketter. Tillväxtverket anger inga enheter för "
    "måtten; enheten för LOGIINTAKT (logiintäkt) är okänd."
)
STATUS_NOTE = (
    'status återger källans text (t.ex. "Sekretesskyddad") för celler där ett '
    "mått har text i stället för ett tal. Tillväxtverkets API definierar inga "
    "sådana markeringar."
)
ZERO_NOTE = (
    "Värdet 0 återges som 0. Källan anger 0 även där ett mått troligen inte "
    "samlas in för anläggningstypen (t.ex. disponibla rum för Camping) och skiljer "
    "inte dessa fall åt."
)


logger = logging.getLogger("tillvaxtverket_logger")


def _labels(columns: list[dict[str, Any]]) -> dict[str, str]:
    return {c["name"]: c["label"] for c in columns if c.get("label")}


def column_records(
    spec: Spec, probe: Probe, labels: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Every source column in source order, with its role and label."""
    roles = {LOAD_DATE: "load_date", spec.time: "time"}
    roles.update(dict.fromkeys(REGION_COLUMNS, "region"))
    roles.update(dict.fromkeys((COUNTY_NAME, MUNICIPALITY_NAME), "region_label"))
    for dimension in spec.dimensions:
        roles[dimension.column] = "dimension"
        roles.update(dict.fromkeys(dimension.label_columns, "category_label"))
    roles.update(dict.fromkeys(spec.measures, "measure"))
    roles.update(dict.fromkeys(spec.attributes, "attribute"))
    records = []
    for name, column_type in probe.columns:
        record = {"name": name, "type": column_type, "role": roles[name]}
        if labels and labels.get(name):
            record["label"] = labels[name]
        records.append(record)
    return records


def _no_elimination() -> dict[str, Any]:
    return {"nordicintel": {"elimination": False}}


def _region_dimension(regions: list[Region]) -> dict[str, Any]:
    codes = [region.code for region in regions]
    known = set(codes)
    children: dict[str, list[str]] = {}
    for region in regions:
        if region.parent in known:
            children.setdefault(region.parent, []).append(region.code)
    category: dict[str, Any] = {
        "index": codes,
        "label": {region.code: region.label for region in regions},
    }
    if children:
        category["child"] = children
    return {
        "label": DIMENSION_LABELS[REGION_DIMENSION],
        "category": category,
        "note": [REGION_NOTE],
        "extension": _no_elimination(),
    }


def _plain_dimension(
    dimension, categories: list[Category], labels: dict[str, str]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": DIMENSION_LABELS.get(dimension.id)
        or labels.get(dimension.column)
        or dimension.column,
        "category": {
            "index": [c.code for c in categories],
            "label": {c.code: c.label for c in categories},
        },
    }
    notes = []
    if dimension.column == COUNTRY_COLUMN:
        notes.append(COUNTRY_NOTE)
    if any(c.code == SUPPRESSED for c in categories):
        notes.append(TYPE_NOTE)
    if notes:
        result["note"] = notes
    result["extension"] = _no_elimination()
    attributes = {c.code: dict(c.attributes) for c in categories if c.attributes}
    if attributes:
        result["extension"][PROVIDER_CODE] = {"categories": attributes}
    return result


def build_cube(
    spec: Spec,
    probe: Probe,
    regions: list[Region],
    categories: dict[str, list[Category]],
    periods: list[str],
    labels: dict[str, str] | None,
) -> dict[str, Any]:
    """The document's cube and identity, without data.europa.eu fields.

    Args:
        spec: The registry entry.
        probe: Columns, row count and load times.
        regions: Region categories from the distinct region columns.
        categories: Categories of each plain dimension, by dimension id.
        periods: Distinct time codes, ascending.
        labels: Column labels from the label file, or None when unavailable
            (labels then fall back to column names).
    """
    names = labels or {}
    if labels is None:
        logger.debug(
            "%s: no column labels available, labels fall back to column names",
            spec.dataset_code,
        )
    dimensions: dict[str, Any] = {REGION_DIMENSION: _region_dimension(regions)}
    for dimension in spec.dimensions:
        dimensions[dimension.id] = _plain_dimension(
            dimension, categories[dimension.id], names
        )
    dimensions[TIME_DIMENSION] = {
        "label": names.get(spec.time) or spec.time,
        "category": {"index": periods, "label": {p: p for p in periods}},
        "extension": _no_elimination(),
    }
    measure_labels = {m: names.get(m) or m for m in spec.measures}
    dimensions[MEASURE_DIMENSION] = {
        "label": DIMENSION_LABELS[MEASURE_DIMENSION],
        "category": {"index": list(spec.measures), "label": measure_labels},
        "note": [MEASURE_NOTE],
        "extension": _no_elimination(),
    }
    ids = list(dimensions)

    nordicintel: dict[str, Any] = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": spec.dataset_code,
        "language": "sv",
        "metadata_url": metadata_url(spec.area, spec.name),
        "data_url": data_url(spec.area, spec.name),
        "time_unit": spec.time_unit,
        "first_period": periods[0],
        "last_period": periods[-1],
    }
    native: dict[str, Any] = {
        "area": spec.area,
        "name": spec.name,
        "region_dimension": REGION_DIMENSION,
        "measure_dimension": MEASURE_DIMENSION,
        "dimension_columns": {
            **{d.id: d.column for d in spec.dimensions},
            TIME_DIMENSION: spec.time,
        },
        "region_columns": list(REGION_COLUMNS),
        "columns": column_records(spec, probe, labels),
        "labels_available": labels is not None,
    }
    if probe.loaded:
        native["updated_original"] = probe.loaded
    native["source_record"] = probe.signal()
    document: dict[str, Any] = {
        "version": "2.0",
        "class": "dataset",
        "label": spec.name,
        "source": SOURCE,
    }
    if probe.updated:
        document["updated"] = probe.updated
    document.update(
        {
            "id": ids,
            "size": [len(dimensions[code]["category"]["index"]) for code in ids],
            "role": {
                "time": [TIME_DIMENSION],
                "geo": [REGION_DIMENSION],
                "metric": [MEASURE_DIMENSION],
            },
            "dimension": dimensions,
            "link": {
                "describedby": [
                    {
                        "href": labels_url(spec.area, spec.name),
                        "label": "Variabeletiketter",
                        "type": "text/csv",
                    },
                    {
                        "href": API_DOCUMENTATION_URL,
                        "label": "DivePort Web API (OpenAPI)",
                        "type": "application/yaml",
                    },
                ]
            },
            "value": [],
            "extension": {"nordicintel": nordicintel, PROVIDER_CODE: native},
        }
    )
    return document


def previous_labels(document: dict[str, Any] | None) -> dict[str, str] | None:
    """Column labels of an accepted document that had the label file."""
    if not document:
        return None
    native = document["extension"][PROVIDER_CODE]
    return _labels(native["columns"]) if native.get("labels_available") else None


def previous_dcat(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if not document:
        return None
    return document["extension"][PROVIDER_CODE].get("dcat")


def _statements(dcat: dict[str, Any]) -> list[str]:
    """Statistics statements in the distributions' Swedish descriptions, such as
    "Statistiken är preliminär under pågående år." (not the access sentences)."""
    found: list[str] = []
    for distribution in dcat.get("distributions") or []:
        for sentence in _SENTENCE.split(str(distribution.get("description_sv") or "")):
            sentence = sentence.strip()
            if "statistik" in sentence.casefold() and sentence not in found:
                found.append(sentence)
    return found


def apply_dcat(
    document: dict[str, Any], spec: Spec, dcat: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a copy of ``document`` with the data.europa.eu fields replaced.

    Without a record (never fetched successfully) the label is the dataset name
    and description, contacts and official statistics status are left out.
    """
    if dcat is None:
        logger.debug(
            "%s: no data.europa.eu record, label is the dataset name and "
            "description and contacts are left out",
            spec.dataset_code,
        )
    result = copy.deepcopy(document)
    nordicintel = result["extension"]["nordicintel"]
    native = result["extension"][PROVIDER_CODE]
    for key in ("description", "official_statistics", "contacts", "doc_url"):
        nordicintel.pop(key, None)
    for key in ("dcat", "english_title", "english_description"):
        native.pop(key, None)
    nordicintel["source_url"] = dcat_page_url(spec.name)
    notes = []
    if dcat is None:
        result["label"] = spec.name
        result["source"] = SOURCE
    else:
        result["label"] = dcat["title"]["sv"].strip()
        result["source"] = (dcat.get("publisher") or {}).get("name") or SOURCE
        notes.extend(_statements(dcat))
        description = str((dcat.get("description") or {}).get("sv") or "").strip()
        if description:
            nordicintel["description"] = description
            if OFFICIAL in description.casefold():
                nordicintel["official_statistics"] = True
        if pages := [p for p in dcat.get("page") or [] if p]:
            nordicintel["doc_url"] = pages[0]
        contacts = []
        organization = (dcat.get("publisher") or {}).get("name")
        for contact in dcat.get("contact_point") or []:
            entry = {}
            if name := str(contact.get("name") or "").strip():
                entry["name"] = name
            email = str(contact.get("email") or "").removeprefix("mailto:").strip()
            if email and not _PLACEHOLDER_EMAIL.match(email):
                entry["email"] = email
            if entry and organization:
                entry["organization"] = organization
            if entry and entry not in contacts:
                contacts.append(entry)
        if contacts:
            nordicintel["contacts"] = contacts
        title_en = (dcat.get("title") or {}).get("en")
        if title_en:
            native["english_title"] = title_en
        description_en = (dcat.get("description") or {}).get("en")
        if description_en:
            native["english_description"] = description_en
        native["dcat"] = dcat
    notes.extend([STATUS_NOTE, ZERO_NOTE])
    result["note"] = notes
    return result

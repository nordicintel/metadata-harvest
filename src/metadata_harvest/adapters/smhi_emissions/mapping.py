"""Map one substance of Nationella emissionsdatabasen to a metadata-only Dataset."""

from typing import Any

from metadata_harvest.adapters.smhi_emissions.api import (
    DATA_URL,
    DOC_URL,
    FILTER_OPTIONS_URL,
    NAMESPACE,
    NATIONAL,
    PROVIDER_CODE,
    SOURCE_URL,
    TOTAL,
    dataset_code,
)
from metadata_harvest.adapters.smhi_emissions.regions import REGIONS
from metadata_harvest.adapters.smhi_emissions.retrieval import (
    CONTENTS,
    REGION,
    SECTOR,
    TIME,
    TOTAL_LABEL,
)
from metadata_harvest.adapters.smhi_emissions.source import (
    FilterOptions,
    Probe,
    Substance,
)

MAPPING_VERSION = 1
"""Increment when a mapping change must be distinguishable in accepted state."""

SOURCE = "SMHI, Nationella emissionsdatabasen"
UNIT = "ton/år"
"""The unit on every chart axis and Excel export of the database (research
2026-10-09); the API response itself carries no unit."""
CONTACT = {
    "organization": "SMHI, Nationella emissionsdatabasen",
    "email": "emissionsdatabasen@smhi.se",
    "url": DOC_URL,
}
"""Published on SMHI's information page (checked 2026-10-09)."""
CO2_EQUIVALENT = "CO2-ekv"

ZERO_NOTE = (
    "Nationella emissionsdatabasen redovisar kombinationer utan utsläpp med värdet "
    "0. En nolla kan därför betyda att utsläppen är noll eller att uppgift saknas "
    "eller inte är tillämplig; databasen skiljer inte på dessa fall och "
    "redovisar inga markeringar (status)."
)
COMPARABILITY_NOTE = (
    "Enligt SMHI överensstämmer siffrorna med Naturvårdsverkets nationella "
    "statistik, förutom för utrikes transporter."
)
EQUIVALENT_NOTE = (
    "Värdena anges i koldioxidekvivalenter (CO2-ekv.), enligt ämnets namn i "
    "Nationella emissionsdatabasen."
)
REQUEST_RULE = (
    "Regionkoder är SCB:s koder. API:t vill ha dem utan inledande nolla: 00 ger "
    "county=0&municipality=0, län NN ger county=NN&municipality=0, kommun NNNN ger "
    "county=NN&municipality=NNNN."
)


def dataset_label(substance: Substance) -> str:
    """``"<long_name>: utsläpp till luft efter region, sektor och år"``."""
    return f"{substance.long_name}: utsläpp till luft efter region, sektor och år"


def _region_dimension() -> dict[str, Any]:
    children: dict[str, list[str]] = {NATIONAL: []}
    for code, _ in REGIONS[1:]:
        if len(code) == 2:
            children[NATIONAL].append(code)
            children[code] = []
        else:
            children[code[:2]].append(code)
    return {
        "label": "region",
        "category": {
            "index": [code for code, _ in REGIONS],
            "label": dict(REGIONS),
            "child": {code: kids for code, kids in children.items() if kids},
        },
        "extension": {
            "nordicintel": {"elimination": True, "elimination_value": NATIONAL}
        },
    }


def _sector_dimension(options: FilterOptions) -> dict[str, Any]:
    index, labels = [TOTAL], {TOTAL: TOTAL_LABEL}
    children: dict[str, list[str]] = {TOTAL: []}
    for sector in options.sectors:
        index.append(sector.code)
        labels[sector.code] = sector.label
        children[TOTAL].append(sector.code)
        if sector.subsectors:
            children[sector.code] = [sub.code for sub in sector.subsectors]
        for sub in sector.subsectors:
            index.append(sub.code)
            labels[sub.code] = sub.label
    return {
        "label": "sektor",
        "category": {"index": index, "label": labels, "child": children},
        "extension": {"nordicintel": {"elimination": True, "elimination_value": TOTAL}},
    }


def build_dataset(
    substance: Substance, options: FilterOptions, probe: Probe
) -> dict[str, Any]:
    """Build the Swedish metadata-only document for one substance."""
    code = substance.code
    years = list(probe.years)
    equivalent = CO2_EQUIVALENT in substance.long_name
    contents: dict[str, Any] = {
        "index": [code],
        "label": {code: substance.long_name},
        "unit": {code: {"label": UNIT}},
    }
    if equivalent:
        contents["note"] = {code: [EQUIVALENT_NOTE]}
    dimensions = {
        REGION: _region_dimension(),
        SECTOR: _sector_dimension(options),
        TIME: {
            "label": "år",
            "category": {"index": years, "label": {year: year for year in years}},
            "extension": {"nordicintel": {"elimination": False}},
        },
        CONTENTS: {
            "label": "tabellinnehåll",
            "category": contents,
            "extension": {"nordicintel": {"elimination": False}},
        },
    }
    ids = list(dimensions)

    notes = []
    if probe.submission is not None:
        notes.append(
            f"Uppgifterna avser SMHI:s rapporteringsomgång (submission) "
            f"{probe.submission}. Varje ny omgång reviderar hela tidsserien."
        )
    notes.append(f"Utsläpp i {UNIT}." + (f" {EQUIVALENT_NOTE}" if equivalent else ""))
    notes.append(
        "Tidsserien är inte heltäckande utan omfattar åren " + ", ".join(years) + "."
    )
    notes.append(COMPARABILITY_NOTE)
    notes.append(ZERO_NOTE)

    nordicintel = {
        "provider_code": PROVIDER_CODE,
        "dataset_code": dataset_code(code),
        "language": "sv",
        "source_url": SOURCE_URL,
        "doc_url": DOC_URL,
        "metadata_url": FILTER_OPTIONS_URL,
        "data_url": DATA_URL,
        "description": (
            f"Utsläpp till luft av {substance.long_name} per län, kommun, "
            f"huvudsektor och undersektor, {UNIT}."
        ),
        "time_unit": "annual",
        "first_period": years[0],
        "last_period": years[-1],
        "contacts": [CONTACT],
    }
    native = {
        "substance": {
            "code": code,
            "substance_id": substance.substance_id,
            "long_name": substance.long_name,
        },
        "submission": probe.submission,
        "sectors": [
            {
                "code": sector.code,
                "label": sector.label,
                "subsectors": [
                    {"code": sub.code, "label": sub.label} for sub in sector.subsectors
                ],
            }
            for sector in options.sectors
        ],
        "listed_in_subsectors": list(substance.listed_in),
        "request_parameters": REQUEST_RULE,
        "source_record": {
            "substance": {
                code: {
                    "substance_id": substance.substance_id,
                    "long_name": substance.long_name,
                }
            },
            "probe_label": probe.label,
            "primary_submission": probe.submission,
            "secondary_submissions": list(probe.secondary_submissions),
        },
    }
    return {
        "version": "2.0",
        "class": "dataset",
        "label": dataset_label(substance),
        "source": SOURCE,
        "note": notes,
        "id": ids,
        "size": [len(dimensions[name]["category"]["index"]) for name in ids],
        "role": {"time": [TIME], "geo": [REGION], "metric": [CONTENTS]},
        "dimension": dimensions,
        "value": [],
        "extension": {"nordicintel": nordicintel, NAMESPACE: native},
    }

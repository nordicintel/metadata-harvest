"""Source model of Nationella emissionsdatabasen: sector tree, substances, probes.

``getfilteroptions/`` returns a list of main sectors, each with subsectors that list
the substances reported for them::

    [{"sector": "Transporter", "code": "4", "subsectors": [
        {"subsector": "Personbilar", "code": "4.1",
         "substances": [{"CO2": {"substance_id": 11, "long_name": "Koldioxid (CO2)"}}]}]}]

A sector with a single subsector reuses the sector code for it (``1``, ``3``); that
subsector is the sector itself. Substances repeat across subsectors; the 29
distinct codes are the datasets.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from retrieval.data_adapters.smhi_emissions import TOTAL_LABEL, resolve_label, series

_SECTOR = re.compile(r"[1-9]\d*")


@dataclass(frozen=True)
class Subsector:
    code: str
    label: str


@dataclass(frozen=True)
class Sector:
    """A main sector; ``subsectors`` excludes a subsector reusing the sector code."""

    code: str
    label: str
    subsectors: tuple[Subsector, ...]


@dataclass(frozen=True)
class Substance:
    code: str
    substance_id: int
    long_name: str
    listed_in: tuple[str, ...]
    """Subsector codes whose ``substances`` list this substance, in tree order."""


@dataclass
class FilterOptions:
    """The parsed tree.

    Attributes:
        sectors: Main sectors in source order.
        substances: Valid, unambiguous substances by code, in first-seen order.
        quarantined: Codes listed with conflicting ``substance_id``/``long_name``.
        problems: ``(resource, error)`` for malformed substance entries.
        complete: False when any entry was malformed or conflicting; such a tree
            cannot prove that a substance was removed.
    """

    sectors: list[Sector]
    substances: dict[str, Substance] = field(default_factory=dict)
    quarantined: dict[str, str] = field(default_factory=dict)
    problems: list[tuple[str, Exception]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems and not self.quarantined


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_filter_options(body: Any) -> FilterOptions:
    """Parse ``getfilteroptions/``.

    The sector tree defines every document's Sektor dimension, so a malformed
    sector or subsector is a failure of the whole tree. A malformed substance entry
    is reported and skipped; later entries are still read.

    Raises:
        ValueError: The tree is not a list of well-formed sectors and subsectors, or
            codes or labels collide.
    """
    if not isinstance(body, list) or not body:
        raise ValueError(f"unexpected getfilteroptions response {str(body)[:200]!r}")
    sectors: list[Sector] = []
    entries: list[tuple[str, int, Any]] = []
    codes: set[str] = set()
    for position, row in enumerate(body):
        if not isinstance(row, Mapping):
            raise ValueError(f"sector {position} is not an object")
        code, label = _text(row.get("code")), _text(row.get("sector"))
        rows = row.get("subsectors")
        if not code or not _SECTOR.fullmatch(code) or not label:
            raise ValueError(f"sector {position} lacks a numeric code or a label")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"sector {code} has no subsectors")
        subsectors: list[Subsector] = []
        for number, sub in enumerate(rows):
            sub_code = _text(sub.get("code")) if isinstance(sub, Mapping) else None
            sub_label = _text(sub.get("subsector")) if sub_code else None
            if not sub_code or not sub_label:
                raise ValueError(f"subsector {number} of sector {code} is malformed")
            if sub_code == code:
                if len(rows) > 1:
                    raise ValueError(f"sector {code} reuses its code for one of many")
            elif not re.fullmatch(rf"{re.escape(code)}\.\d+", sub_code):
                raise ValueError(f"subsector {sub_code!r} is not under sector {code}")
            else:
                subsectors.append(Subsector(sub_code, sub_label))
            if sub_code in codes and sub_code != code:
                raise ValueError(f"duplicate sector code {sub_code!r}")
            codes.add(sub_code)
            substances = sub.get("substances")
            if not isinstance(substances, list):
                raise ValueError(f"subsector {sub_code} has no substance list")
            entries += [(sub_code, i, entry) for i, entry in enumerate(substances)]
        if code in {sector.code for sector in sectors}:
            raise ValueError(f"duplicate sector code {code!r}")
        labels = [s.label for s in subsectors]
        if len(set(labels)) != len(labels) or TOTAL_LABEL in labels:
            raise ValueError(f"sector {code} has ambiguous subsector labels")
        sectors.append(Sector(code, label, tuple(subsectors)))
    labels = [sector.label for sector in sectors]
    if len(set(labels)) != len(labels) or TOTAL_LABEL in labels:
        raise ValueError("main sector labels are ambiguous")

    options = FilterOptions(sectors)
    seen: dict[str, dict[tuple[int, str], list[str]]] = {}
    for sub_code, position, entry in entries:
        resource = f"filteroptions/{sub_code}/{position}"
        try:
            if not isinstance(entry, Mapping) or len(entry) != 1:
                raise ValueError("substance entry is not a one-key object")
            ((code, details),) = entry.items()
            name = (
                _text(details.get("long_name"))
                if isinstance(details, Mapping)
                else None
            )
            number = details.get("substance_id") if name else None
            if not _text(code) or code != code.strip() or "/" in code:
                raise ValueError(f"invalid substance code {code!r}")
            if not name or not isinstance(number, int) or isinstance(number, bool):
                raise ValueError(f"substance {code!r} lacks substance_id or long_name")
        except ValueError as error:
            options.problems.append((resource, error))
            continue
        seen.setdefault(code, {}).setdefault((number, name), []).append(sub_code)
    for code, variants in seen.items():
        if len(variants) > 1:
            options.quarantined[code] = (
                f"substance {code!r} is listed with conflicting identities "
                f"{sorted(variants)}"
            )
            continue
        ((number, name), listed_in) = next(iter(variants.items()))
        options.substances[code] = Substance(code, number, name, tuple(listed_in))
    return options


@dataclass(frozen=True)
class Probe:
    """What the national ``hsektor=0`` request reveals about one substance.

    Attributes:
        label: The response's substance key (SMHI's long name).
        years: Every year of any series, sorted.
        submission: ``primary_submission``: the reporting round served, a year.
        secondary_submissions: ``secondary_submissions`` as returned.
    """

    label: str
    years: tuple[str, ...]
    submission: int | None
    secondary_submissions: tuple[Any, ...]


def parse_probe(body: Any, options: FilterOptions, substance: str) -> Probe:
    """Validate a national probe against the tree and return its years.

    Raises:
        ValueError: The response names an unknown sector, has no years, or does not
            have the documented shape.
    """
    try:
        by_label = series(body, f"national probe of {substance}")
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    data = body["data"]
    if len(data) != 1:
        raise ValueError(f"probe of {substance} names no substance")
    known = {
        label: label for label in (TOTAL_LABEL, *(s.label for s in options.sectors))
    }
    if unknown := sorted(
        label for label in by_label if not resolve_label(known, label)
    ):
        raise ValueError(f"probe of {substance} has unknown sector labels {unknown}")
    years = sorted({year for points in by_label.values() for year, _ in points})
    if not years:
        raise ValueError(f"probe of {substance} returned no years")
    submission = body.get("primary_submission")
    if submission is not None and (
        not isinstance(submission, int) or isinstance(submission, bool)
    ):
        raise ValueError(f"unexpected primary_submission {submission!r}")
    secondary = body.get("secondary_submissions") or []
    if not isinstance(secondary, list):
        raise ValueError(f"unexpected secondary_submissions {secondary!r}")
    return Probe(str(next(iter(data))), tuple(years), submission, tuple(secondary))

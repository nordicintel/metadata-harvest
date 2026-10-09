"""Live retrieval from SMHI's Nationella emissionsdatabasen (NordicIntel contract).

Imports only ``aiohttp`` and the standard library. The service calls
:func:`retrieve` through the ``nordicintel.retrieval`` entry point ``smhi``.

``GET getdata/?county=&municipality=&hsektor=&usektor=&sub=`` returns one
region's series for one substance: ``hsektor=0`` gives ``Totalt`` plus every main
sector, ``hsektor=<sector>`` gives that sector's ``Totalt`` plus its subsectors.
Series are keyed by sector *label*, which the harvested sector tree maps back to
codes. The API fills combinations without emissions with ``0.0``; those values are
returned as provided, because they cannot be told apart from real zeros.
"""

import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

PROVIDER_CODE = "smhi"
NAMESPACE = "smhi_emissions"
"""Adapter namespace of the harvested documents (``extension.smhi_emissions``)."""
API_ROOT = "https://nationellaemissionsdatabasen.smhi.se/api"
FILTER_OPTIONS_URL = f"{API_ROOT}/getfilteroptions/"
DATA_URL = f"{API_ROOT}/getdata/"
DATASET_PREFIX = "emissionsdatabasen_"

REGION, SECTOR, TIME, CONTENTS = "Region", "Sektor", "Tid", "ContentsCode"
TOTAL = "0"
"""Sector code of ``Totalt``: the API's ``hsektor=0`` (all sectors)."""
TOTAL_LABEL = "Totalt"
NATIONAL = "00"

REQUIRED_METADATA = ("/id", f"/extension/{NAMESPACE}")
"""The dataset's dimension ids and its ``extension.smhi_emissions``: the substance
code and the harvested sector tree (``sectors``) that maps response labels to codes."""

MAX_REQUESTS = 50
"""Most ``getdata`` requests one call may make (one per region and sector group,
sent one at a time). An adapter bound on call duration, not a documented limit:
a larger selection raises ``ValueError``."""

_SERVER_ERROR = "Server Error (500)"
"""Django's generic HTTP 500 page, also returned for an unknown substance code."""
_REGION = re.compile(r"\d{2}|\d{4}")

logger = logging.getLogger(__name__)


def dataset_code(substance: str) -> str:
    return f"{DATASET_PREFIX}{substance}"


def region_parameters(code: str) -> dict[str, str]:
    """Return ``county``/``municipality`` for an SCB region code.

    ``00`` (Sverige) is ``0``/``0``; a län ``NN`` is ``int(NN)``/``0``; a kommun
    ``NNNN`` is ``int(NN)``/``int(NNNN)``: the API wants no leading zeros.

    Raises:
        ValueError: ``code`` is not a two- or four-digit SCB code.
    """
    if not _REGION.fullmatch(code):
        raise ValueError(f"not an SCB region code: {code!r}")
    if code == NATIONAL:
        return {"county": "0", "municipality": "0"}
    county = str(int(code[:2]))
    return {"county": county, "municipality": str(int(code)) if len(code) == 4 else "0"}


def data_parameters(region: str, sector: str, substance: str) -> dict[str, str]:
    """The ``getdata`` query for one region, sector group (``0`` = all) and substance."""
    return {
        **region_parameters(region),
        "hsektor": sector,
        "usektor": "0",
        "sub": substance,
    }


def _number(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def series(body: Any, context: str) -> dict[str, list[tuple[str, float | int]]]:
    """Return ``{sector label: [(year, value), ...]}`` from a ``getdata`` body.

    An unknown region answers ``{"data": {<substance>: {}}}``: no series.

    Raises:
        RuntimeError: The body does not have the documented shape.
    """
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, Mapping) or len(data) > 1:
        raise RuntimeError(f"unexpected getdata response for {context}")
    result: dict[str, list[tuple[str, float | int]]] = {}
    for by_sector in data.values():
        if not isinstance(by_sector, Mapping):
            raise RuntimeError(f"unexpected getdata series for {context}")
        for label, points in by_sector.items():
            if not isinstance(points, list):
                raise RuntimeError(f"unexpected series {label!r} for {context}")
            parsed = []
            for point in points:
                year = point.get("year") if isinstance(point, Mapping) else None
                value = point.get("value") if isinstance(point, Mapping) else None
                if not re.fullmatch(r"\d{4}", str(year)) or not _number(value):
                    raise RuntimeError(f"unexpected point {point!r} for {context}")
                parsed.append((str(year), value))
            result[str(label)] = parsed
    return result


def sector_labels(sectors: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Label-to-code maps per request group: ``"0"`` and each sector with subsectors.

    In the ``hsektor=0`` response ``Totalt`` is the grand total ``0``; in a sector's
    response ``Totalt`` repeats that sector and is not used.
    """
    groups = {TOTAL: {TOTAL_LABEL: TOTAL}}
    for sector in sectors:
        groups[TOTAL][str(sector["label"])] = str(sector["code"])
        subsectors = sector.get("subsectors") or []
        if subsectors:
            groups[str(sector["code"])] = {
                str(sub["label"]): str(sub["code"]) for sub in subsectors
            }
    return groups


def resolve_label(labels: Mapping[str, str], label: str) -> str | None:
    """The code of ``label``: an exact match, else the only case-insensitive one.

    Submission 2025 spelled ``inrikes civil sjöfart`` where the 2026 tree has
    ``Inrikes civil sjöfart``; capitalisation alone does not change a sector.
    """
    if label in labels:
        return labels[label]
    matches = [
        code for name, code in labels.items() if name.casefold() == label.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


async def _substance_listed(
    session: aiohttp.ClientSession, url: str, substance: str
) -> bool:
    async with session.get(url) as response:
        response.raise_for_status()
        tree = await response.json(content_type=None)
    return any(
        substance in entry
        for sector in tree
        for subsector in sector.get("subsectors") or []
        for entry in subsector.get("substances") or []
        if isinstance(entry, Mapping)
    )


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, str],
    metadata_url: str,
) -> Any:
    error, server_error = None, False
    async with session.get(url, params=params) as response:
        if response.status == 500:
            server_error = _SERVER_ERROR in await response.text()
            error = aiohttp.ClientResponseError(
                response.request_info,
                response.history,
                status=response.status,
                message=response.reason or "",
                headers=response.headers,
            )
        else:
            response.raise_for_status()
            body = await response.json(content_type=None)
    if error is not None:
        # An unknown ``sub`` gives Django's generic 500; only a substance missing
        # from getfilteroptions proves the dataset is gone.
        if server_error and not await _substance_listed(
            session, metadata_url, params["sub"]
        ):
            raise LookupError(
                f"SMHI no longer lists substance {params['sub']!r}"
            ) from error
        raise error
    logger.debug("GET %s %s", url, params)
    return body


async def retrieve(
    session: aiohttp.ClientSession,
    *,
    provider_code: str,
    dataset_code: str,
    language: str,
    selection: Mapping[str, Sequence[str]],
    data_url: str | None,
    metadata_url: str | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the observation fragment for ``selection`` of one substance dataset.

    One request is made per selected region for ``Totalt`` and main sectors
    (``hsektor=0``), plus one per region and main sector whose subsectors are
    selected. Requests are sent one at a time. Values are numbers exactly as SMHI
    returns them (``0.0`` included); no ``status`` is produced; cells SMHI returns
    no point for are left out.

    Raises:
        ValueError: The selection lacks a dimension, names another substance or an
            unknown sector or region code, or needs more than :data:`MAX_REQUESTS`.
        LookupError: SMHI no longer lists the substance.
    """
    native = metadata.get(f"/extension/{NAMESPACE}") or {}
    substance = str(
        (native.get("substance") or {}).get("code")
        or dataset_code.removeprefix(DATASET_PREFIX)
    )
    sectors = native.get("sectors")
    if not sectors:
        raise ValueError(f"metadata lacks the harvested sector tree of {dataset_code}")
    ids = list(metadata.get("/id") or (REGION, SECTOR, TIME, CONTENTS))
    if missing := [name for name in ids if name not in selection]:
        raise ValueError(f"selection must include every dimension; missing {missing}")
    if unknown := [name for name in selection if name not in ids]:
        raise ValueError(f"unknown dimensions {unknown}")
    if list(selection[CONTENTS]) != [substance]:
        raise ValueError(f"{CONTENTS} must select {substance!r} only")

    groups = sector_labels(sectors)
    parent = {
        code: group for group, labels in groups.items() for code in labels.values()
    }
    parent[TOTAL] = TOTAL
    wanted_sectors = set(selection[SECTOR])
    if unknown := sorted(wanted_sectors - parent.keys()):
        raise ValueError(f"unknown sector codes {unknown}")
    needed = [group for group in groups if group in {parent[c] for c in wanted_sectors}]
    regions = list(selection[REGION])
    queries = [
        (region, group, data_parameters(region, group, substance))
        for region in regions
        for group in needed
    ]
    if len(queries) > MAX_REQUESTS:
        raise ValueError(
            f"selection needs {len(queries)} requests; at most {MAX_REQUESTS} "
            "(one per region and sector group) are allowed"
        )
    url = data_url or DATA_URL
    listing = metadata_url or FILTER_OPTIONS_URL
    years = set(selection[TIME])
    known_labels = {label: label for labels in groups.values() for label in labels}

    cells: dict[tuple[str, str, str], float | int] = {}
    for region, group, params in queries:
        body = await _fetch(session, url, params, listing)
        for label, points in series(body, f"{region}/{group}").items():
            code = resolve_label(groups[group], label)
            if code is None:  # e.g. a sector's own "Totalt": taken from hsektor=0
                if resolve_label(known_labels, label) is None:
                    raise RuntimeError(
                        f"SMHI returned unknown sector label {label!r} for "
                        f"{substance} {params}; the harvested sector tree is outdated"
                    )
                continue
            if code not in wanted_sectors:
                continue
            for year, value in points:
                if year not in years:
                    continue
                key = (region, code, year)
                if key in cells and cells[key] != value:
                    raise RuntimeError(f"SMHI returned conflicting values for {key}")
                cells[key] = value

    positions = {
        name: {code: i for i, code in enumerate(selection[name])} for name in ids
    }
    sizes = [len(selection[name]) for name in ids]
    value: dict[str, float | int] = {}
    for (region, sector, year), cell in cells.items():
        coordinates = {REGION: region, SECTOR: sector, TIME: year, CONTENTS: substance}
        index = 0
        for name, size in zip(ids, sizes, strict=True):
            index = index * size + positions[name][coordinates[name]]
        value[str(index)] = cell
    total = math.prod(sizes)
    return {
        "id": ids,
        "size": sizes,
        "dimension": {
            name: {"category": {"index": list(selection[name])}} for name in ids
        },
        "value": (
            [value[str(i)] for i in range(total)]
            if len(value) == total
            else dict(sorted(value.items(), key=lambda item: int(item[0])))
        ),
    }

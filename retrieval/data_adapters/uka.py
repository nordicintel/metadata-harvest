"""Live retrieval of UKÄ observations (NordicIntel retrieval contract).

Imports only ``aiohttp`` and the standard library. The service calls
:func:`retrieve` through the ``nordicintel.retrieval`` entry point ``uka``.
"""

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import aiohttp

from retrieval._uka_export import (
    API_ROOT,
    EXPORT_PARAMS,
    NO_PERIOD_ERROR,
    TOTAL,
    read_export,
)

REQUIRED_METADATA = ("/role",)
"""The dataset's roles: ``role.time`` names the period dimension, whose codes pick
the export files to download."""

CONCURRENCY = 4
"""Most periods fetched at once for one selection."""

logger = logging.getLogger("uka_logger")


def export_request(
    dataset_code: str, data_url: str | None, period: str
) -> tuple[str, dict[str, str]]:
    """Return the URL and query parameters that ask for one period's export link."""
    split = urlsplit(data_url or f"{API_ROOT}/totals/{dataset_code}")
    params = dict(parse_qsl(split.query)) or dict(EXPORT_PARAMS)
    params["year"] = period
    return urlunsplit(split._replace(query="")), params


async def export_link(
    session: aiohttp.ClientSession, url: str, params: Mapping[str, str]
) -> str:
    """Return the file URL UKÄ answers for an export request.

    Raises:
        LookupError: The indicator no longer exists (HTTP 404) or has no data.
        ValueError: The indicator has no data for the requested period.
    """
    async with session.get(url, params=params) as response:
        if response.status == 404:
            raise LookupError(f"UKÄ has no indicator at {url}")
        response.raise_for_status()
        try:
            body = await response.json(content_type=None)
        except ValueError as error:
            raise RuntimeError(f"UKÄ answered {url} with non-JSON") from error
    if not isinstance(body, dict):
        raise RuntimeError(f"UKÄ answered {url} {dict(params)} with {body!r}")
    if isinstance(error := body.get("error"), str):
        if error.startswith(NO_PERIOD_ERROR):
            raise ValueError(f"UKÄ rejected period {params.get('year')!r}: {error}")
        raise LookupError(f"UKÄ indicator at {url} is unavailable: {error}")
    link = body.get("url")
    if not isinstance(link, str) or not link:
        raise RuntimeError(f"UKÄ answered {url} {dict(params)} without a file link")
    return link


async def download_text(session: aiohttp.ClientSession, url: str) -> str:
    """Return a UTF-8 export file's text."""
    async with session.get(url) as response:
        response.raise_for_status()
        content = await response.read()
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{url} is not UTF-8") from error


Cell = tuple[int | float | None, str | None]


def match_export(
    text: str,
    period: str,
    time: str,
    selection: Mapping[str, Sequence[str]],
    link: str,
) -> dict[int, Cell]:
    """Return the cells of one period's export that ``selection`` asks for.

    Keys are flat observation indices (selection order, last dimension fastest).
    An omitted dimension must be ``Total``. The period is the first column whatever
    its header says: a few older exports misspell it (``Lasar``, ``wÅr``).

    Raises:
        RuntimeError: The export is malformed, lacks a selected dimension, has a
            row for another period, or gives a selected cell twice with different
            values (UKÄ does so for a few cells, listed in the harvested
            ``extension.uka.conflicting_cells``).
    """
    ids = list(selection)
    positions = {
        name: {code: i for i, code in enumerate(selection[name])} for name in ids
    }
    sizes = [len(selection[name]) for name in ids]
    cells: dict[int, Cell] = {}
    try:
        layout, rows = read_export(text)
        columns = layout.dimension_codes
        if unknown := set(selection) - {time, *columns}:
            raise RuntimeError(
                f"{link}: export lacks dimensions {sorted(unknown)}; "
                "the export structure changed since harvesting"
            )
        for row in rows:
            if row.period != period:
                raise RuntimeError(f"{link}: row for period {row.period!r}")
            codes = dict(zip(columns, row.categories, strict=True))
            codes[time] = period
            if any(
                codes[name] != TOTAL for name in columns if name not in selection
            ) or any(codes[name] not in positions[name] for name in ids):
                continue
            index = 0
            for name, size in zip(ids, sizes, strict=True):
                index = index * size + positions[name][codes[name]]
            cell = (row.value, row.marker)
            if cells.setdefault(index, cell) != cell:
                raise RuntimeError(f"{link}: conflicting rows for {codes}")
    except ValueError as error:
        raise RuntimeError(f"{link}: malformed UKÄ export: {error}") from error
    return cells


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
    """Return the observation fragment for ``selection`` of one UKÄ indicator.

    Downloads the CSV export of every selected period (one link request and one
    file per period) and keeps the rows matching the selection. An omitted dimension
    is answered with its ``Total`` rows. Values are numbers; marker cells (such as
    ``..``) are ``null`` with the marker in ``status``; cells without a row are left
    out.

    Raises:
        ValueError: The selection lacks the period dimension, or UKÄ has no data
            for a selected period.
        LookupError: The indicator no longer exists at UKÄ.
        RuntimeError: UKÄ's answer or export is malformed, no longer has a
            selected dimension, or gives a selected cell twice with different
            values (see :func:`match_export`).
    """
    roles = metadata.get("/role") or {}
    time = next(iter(roles.get("time") or []), None)
    if time is None or time not in selection:
        raise ValueError(f"selection must include the period dimension {time!r}")

    ids = list(selection)
    sizes = [len(selection[name]) for name in ids]
    cells: dict[int, Cell] = {}
    limit = asyncio.Semaphore(CONCURRENCY)

    async def fetch(period: str) -> None:
        url, params = export_request(dataset_code, data_url, period)
        async with limit:
            link = await export_link(session, url, params)
            text = await download_text(session, link)
        # Parsing a full-cube export takes seconds; keep it off the event loop.
        found = await asyncio.to_thread(
            match_export, text, period, time, selection, link
        )
        logger.debug("%s %s: %d cells from %s", dataset_code, period, len(found), link)
        cells.update(found)  # periods occupy disjoint indices

    tasks = [asyncio.ensure_future(fetch(period)) for period in selection[time]]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:  # a failed retrieval returns nothing; stop the rest
            task.cancel()
        raise

    total = math.prod(sizes)
    fragment: dict[str, Any] = {
        "id": ids,
        "size": sizes,
        "dimension": {
            name: {"category": {"index": list(selection[name])}} for name in ids
        },
        "value": (
            [cells[i][0] for i in range(total)]
            if len(cells) == total
            else {str(i): cells[i][0] for i in sorted(cells)}
        ),
    }
    if status := {str(i): cells[i][1] for i in sorted(cells) if cells[i][1]}:
        fragment["status"] = status
    return fragment

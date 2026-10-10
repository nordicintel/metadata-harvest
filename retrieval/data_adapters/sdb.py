"""Live retrieval of Socialstyrelsen SDB observations.

Implements the NordicIntel adapter retrieval contract (``docs/RETRIEVAL.md`` in
``nordicintel-schemas``). This module must only import ``aiohttp`` and the standard
library so that it works without the ``harvest`` extra.
"""

import asyncio
import itertools
import logging
import math
import re
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

API_ROOT = "https://sdb.socialstyrelsen.se/api/v1"
PAGE_SIZE = 5000
"""Largest page size the SDB API honours."""
MAX_SEGMENT_LENGTH = 250
"""SDB rejects URL path segments longer than about 256 characters with HTTP 400."""
MAX_CONCURRENT_REQUESTS = 4
RESULT_DIMENSIONS_POINTER = "/extension/socialstyrelsen/result_dimensions"
"""Harvested map of dimension codes to differing SDB result field names."""
REQUIRED_METADATA: tuple[str, ...] = (RESULT_DIMENSIONS_POINTER,)

_ACCEPT_JSON = {"Accept": "application/json"}
_INTEGER = re.compile(r"-?\d+")
_DECIMAL = re.compile(r"-?\d+[,.]\d+")

logger = logging.getLogger("sdb_logger")


def subject_url(language: str, subject: str) -> str:
    """Return the SDB URL listing a subject's variables."""
    return f"{API_ROOT}/{quote(language, safe='')}/{quote(subject, safe='')}"


def result_url(language: str, subject: str) -> str:
    """Return the SDB observation endpoint for a subject."""
    return f"{subject_url(language, subject)}/resultat"


def to_https(url: str) -> str:
    """Return an SDB ``url`` with an ``https`` scheme; SDB links often use ``http``."""
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.hostname == urlsplit(API_ROOT).hostname:
        parts = parts._replace(scheme="https")
    return urlunsplit(parts).rstrip("/")


def parse_value(raw: object) -> tuple[int | float | None, str | None]:
    """Parse an SDB ``varde`` into an observation and an optional status flag.

    Integer and decimal-comma strings become numbers. Empty values are missing.
    Any other text is kept as the cell's status flag with a missing value.
    """
    if raw is None or isinstance(raw, bool):
        return None, None
    if isinstance(raw, int | float):
        return raw, None
    text = str(raw).strip()
    if not text:
        return None, None
    compact = text.replace(" ", "").replace(" ", "")
    if _INTEGER.fullmatch(compact):
        return int(compact), None
    if _DECIMAL.fullmatch(compact):
        return float(compact.replace(",", ".")), None
    return None, text


def chunk_codes(
    codes: Sequence[str], max_length: int = MAX_SEGMENT_LENGTH
) -> list[list[str]]:
    """Split category codes into groups whose encoded, comma-joined length fits."""
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for code in codes:
        encoded = len(quote(code, safe=""))
        added = encoded + 1 if current else encoded
        if current and length + added > max_length:
            chunks.append(current)
            current, length, added = [], 0, encoded
        current.append(code)
        length += added
    if current:
        chunks.append(current)
    return chunks


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
    """Fetch the selected observations of an SDB subject.

    Args:
        session: Caller-owned session used for every request.
        provider_code: NordicIntel provider code (``socialstyrelsen``).
        dataset_code: SDB subject name.
        language: Document language, ``sv`` or ``en``.
        selection: Dimension codes mapped to the category codes to return.
        data_url: Harvested SDB result endpoint, if known.
        metadata_url: Harvested SDB subject endpoint, if known.
        metadata: Resolved ``REQUIRED_METADATA``: the result field names of
            dimensions that SDB's data API names differently, if any.

    Returns:
        A retrieval result fragment: ``id``, ``size``, category indices, a sparse
        ``value`` holding exactly the cells SDB returned and, when SDB reports
        flags, a sparse ``status``. Cells SDB does not return are omitted.

    Raises:
        ValueError: The selection is empty or omits a dimension; SDB cannot
            eliminate dimensions.
        LookupError: The subject no longer exists at SDB.
        RuntimeError: SDB returned rows that do not match the request.
    """
    dimensions = list(selection)
    if not dimensions:
        raise ValueError("The selection must include at least one dimension.")
    codes = {
        dimension: [str(code) for code in selection[dimension]]
        for dimension in dimensions
    }
    for dimension, dimension_codes in codes.items():
        if not dimension_codes:
            raise ValueError(f"No categories selected for dimension {dimension!r}.")

    renamed = metadata.get(RESULT_DIMENSIONS_POINTER) or {}
    fields = {
        dimension: str(renamed.get(dimension, dimension)) for dimension in dimensions
    }
    base = to_https(data_url) if data_url else result_url(language, dataset_code)
    chunked = [
        chunk_codes(codes[dimension], MAX_SEGMENT_LENGTH) for dimension in dimensions
    ]
    urls = [
        base
        + _selection_path([fields[dimension] for dimension in dimensions], combination)
        for combination in itertools.product(*chunked)
    ]
    logger.debug(
        "Retrieving %s/%s: %d dimensions, %s categories, %d request URLs "
        "(selection chunked to keep path segments under %d characters)",
        language,
        dataset_code,
        len(dimensions),
        {dimension: len(codes[dimension]) for dimension in dimensions},
        len(urls),
        MAX_SEGMENT_LENGTH,
    )
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    pages = await _gather(_fetch_rows(session, url, semaphore) for url in urls)

    if any(rows is None for rows in pages):
        subject = (
            to_https(metadata_url)
            if metadata_url
            else subject_url(language, dataset_code)
        )
        logger.debug(
            "%s/%s: %d of %d requests answered 404; checking that the subject "
            "still exists",
            language,
            dataset_code,
            sum(rows is None for rows in pages),
            len(pages),
        )
        if not await _exists(session, subject):
            logger.warning("SDB subject %s/%s no longer exists", language, dataset_code)
            raise LookupError(
                f"SDB subject {dataset_code!r} ({language}) no longer exists."
            )

    logger.debug(
        "%s/%s: received %d rows from %d requests",
        language,
        dataset_code,
        sum(len(rows or ()) for rows in pages),
        len(pages),
    )
    return _build_fragment(
        dimensions, codes, fields, (row for rows in pages for row in rows or ())
    )


def _selection_path(dimensions: Sequence[str], chunks: Sequence[Sequence[str]]) -> str:
    return "".join(
        f"/{quote(dimension, safe='')}/{','.join(quote(code, safe='') for code in chunk)}"
        for dimension, chunk in zip(dimensions, chunks, strict=True)
    )


async def _fetch_rows(
    session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore
) -> list[dict[str, Any]] | None:
    """Return all rows for one request, or None when SDB answers 404 (no rows)."""
    async with semaphore:
        first = await _get_json(session, url, {"per_sida": PAGE_SIZE})
    if first is None:
        return None
    rows = list(first["data"])
    page_count = int(first.get("sidor") or 1)
    if page_count > 1:
        logger.debug(
            "%s has %d pages; fetching pages 2-%d", url, page_count, page_count
        )

    async def fetch_page(page: int) -> list[dict[str, Any]]:
        async with semaphore:
            body = await _get_json(session, url, {"per_sida": PAGE_SIZE, "sida": page})
        if body is None:
            raise RuntimeError(f"SDB page {page} of {url} returned 404.")
        return body["data"]

    for page_rows in await _gather(
        fetch_page(page) for page in range(2, page_count + 1)
    ):
        rows.extend(page_rows)
    return rows


async def _get_json(
    session: aiohttp.ClientSession, url: str, params: Mapping[str, int]
) -> dict[str, Any] | None:
    logger.debug("GET %s %s", url, params)
    async with session.get(url, params=params, headers=_ACCEPT_JSON) as response:
        if response.status == 404:
            logger.debug("GET %s answered 404 (no rows)", url)
            return None
        response.raise_for_status()
        return await response.json(content_type=None)


async def _exists(session: aiohttp.ClientSession, url: str) -> bool:
    async with session.get(url, headers=_ACCEPT_JSON) as response:
        if response.status == 404:
            return False
        response.raise_for_status()
        return True


async def _gather[T](awaitables: Iterable[Awaitable[T]]) -> list[T]:
    """Await all, cancelling the rest if one fails, and re-raise the original error."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def result_field(key: str) -> str:
    """Return the SDB field name of a result row key (``regionId`` -> ``region``)."""
    return key.removesuffix("Id")


def _build_fragment(
    dimensions: list[str],
    codes: dict[str, list[str]],
    fields: Mapping[str, str],
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    size = [len(codes[dimension]) for dimension in dimensions]
    positions = {
        dimension: {code: index for index, code in enumerate(codes[dimension])}
        for dimension in dimensions
    }
    strides = [math.prod(size[index + 1 :]) for index in range(len(size))]
    values: dict[int, int | float | None] = {}
    flags: dict[int, str] = {}
    expected = set(dimensions)
    dimension_of = {field: dimension for dimension, field in fields.items()}

    for row in rows:
        row_codes = {
            dimension_of.get(field, field): value
            for key, value in row.items()
            if key != "varde" and (field := result_field(key))
        }
        if extra := row_codes.keys() - expected:
            raise ValueError(
                f"SDB returned dimensions {sorted(extra)} that are not in the "
                "selection; SDB cannot eliminate dimensions, so select them explicitly."
            )
        if missing := expected - row_codes.keys():
            raise RuntimeError(
                f"SDB row lacks dimensions {sorted(missing)}: {dict(row)}"
            )
        cell = 0
        for dimension, stride in zip(dimensions, strides, strict=True):
            code = str(row_codes[dimension])
            if code not in positions[dimension]:
                raise RuntimeError(
                    f"SDB returned unrequested category {code!r} for {dimension!r}."
                )
            cell += positions[dimension][code] * stride
        value, flag = parse_value(row.get("varde"))
        if cell in values and (values[cell], flags.get(cell)) != (value, flag):
            raise RuntimeError(f"SDB returned conflicting values for cell {row_codes}.")
        values[cell] = value
        if flag is not None:
            flags[cell] = flag

    fragment: dict[str, Any] = {
        "id": dimensions,
        "size": size,
        "dimension": {
            dimension: {"category": {"index": codes[dimension]}}
            for dimension in dimensions
        },
        "value": {str(cell): values[cell] for cell in sorted(values)},
    }
    if flags:
        fragment["status"] = {str(cell): flags[cell] for cell in sorted(flags)}
    return fragment

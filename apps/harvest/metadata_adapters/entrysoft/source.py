"""Read a RowStore table's rows from the files it was built from.

A RowStore distribution's ``dcterms:source`` names the distribution holding the
uploaded file(s), e.g. 26 monthly CSV parts for Ale's supplier ledger. Its
``dcat:downloadURL`` (or ``dcat:accessURL``) values are the files. Downloading
them costs one request per file instead of one per 500 rows.

The result is only used if it provably matches the table: the same row count as
RowStore reports, and every value from RowStore's first page present in the
files. Anything else falls back to reading RowStore page by page.
"""

from __future__ import annotations

import csv
import io
import logging

import httpx

from apps.harvest.metadata_adapters.entrysoft.client import Client

logger = logging.getLogger("entrysoft_logger")

DCAT = "http://www.w3.org/ns/dcat#"
DELIMITERS = (",", ";", "\t", "|")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            logger.debug("Source file is not valid %s; trying the next one", encoding)
            continue
    raise ValueError("Source file is neither UTF-8 nor Windows-1252 text")


def parse_csv(data: bytes) -> list[dict[str, str]]:
    """Parse a delimited text file, choosing the delimiter that splits the header most."""
    text = _decode(data)
    header = text.splitlines()[0] if text else ""
    delimiter = max(DELIMITERS, key=header.count)
    return list(csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter))


async def source_file_urls(client: Client, source_uri: str) -> list[str]:
    """Download URLs of the source distribution, from its EntryStore metadata."""
    if "/resource/" not in source_uri:
        return []
    metadata = await client.get(source_uri.replace("/resource/", "/metadata/"))
    properties = metadata.get(source_uri) or {}
    urls = [o.get("value") for o in properties.get(DCAT + "downloadURL", [])]
    if not urls:
        urls = [o.get("value") for o in properties.get(DCAT + "accessURL", [])]
        logger.debug("%s lists no downloadURL; using accessURL values", source_uri)
    return [url for url in urls if url]


async def load_source_rows(
    client: Client, source_uri: str, max_files: int
) -> tuple[list[dict[str, str]] | None, str]:
    """All rows of the source files, or ``None`` and the reason they cannot be used."""
    try:
        urls = await source_file_urls(client, source_uri)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403, 404, 410):
            return (
                None,
                f"source metadata not public (HTTP {error.response.status_code})",
            )
        raise
    if not urls:
        return None, "source distribution lists no download or access URLs"
    if len(urls) > max_files:
        return (
            None,
            f"{len(urls)} source files, more requests than paging ({max_files} allowed)",
        )
    logger.debug("Reading %d source file(s) of %s", len(urls), source_uri)
    rows: list[dict[str, str]] = []
    for url in urls:
        try:
            content_type, data = await client.get_bytes(url)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (401, 403, 404, 410):
                return (
                    None,
                    f"source file not public (HTTP {error.response.status_code})",
                )
            raise
        textual = "csv" in content_type or "text/plain" in content_type
        # Some instances serve CSV as application/octet-stream; Excel and zip files
        # start with "PK", and binary data contains NUL bytes.
        if not textual and (data[:2] == b"PK" or b"\x00" in data[:4096]):
            return (
                None,
                f"source file is {content_type or 'of unknown type'} and binary, not CSV",
            )
        parsed = parse_csv(data)
        logger.debug("Source file %s: %d rows", url, len(parsed))
        rows.extend(parsed)
    return rows, "ok"

"""Discover every RowStore table on an EntryScape instance through EntryStore search.

1. A Solr search for distributions whose metadata references a RowStore URL.
   Each hit carries the table URL (``dcat:accessURL``), its own title and
   modified date, and, under ``relations``, the dataset that lists it as a
   ``dcat:distribution``.
2. The referenced datasets only (titles, descriptions, dates, publisher), looked
   up in batches by resource URI, with the per-entry metadata endpoint as a
   fallback for any the search does not return.

EntryStore search paging is unreliable on several instances (checked
2026-10-09): pages are capped at 100, the reported total changes between pages
(and can be negative), and offsets repeat or skip hits whatever the sort order.
The set of distinct hits is stable, though. Searches therefore ignore totals and
deduplicate by entry. Results that fit on one page are taken as they are; longer
results are re-read in whole passes until a pass finds nothing new.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx

from harvesting.adapters.entrysoft.client import (
    BudgetExhausted,
    Client,
    RateLimited,
)
from harvesting.models import SourceIdentity
from harvesting.sources import catalog_configurations

SEARCH_PATH = "store/search"
SEARCH_PAGE = 100
DCAT = "http://www.w3.org/ns/dcat#"
DCTERMS = "http://purl.org/dc/terms/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
DISTRIBUTION_QUERY = r"rdfType:http\://www.w3.org/ns/dcat#Distribution AND metadata.object.uri:*rowstore*"
TABLE_URL = re.compile(
    r"^https?://[^/]+/rowstore/dataset/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def within_catalog_scope(provider: dict, record: dict) -> bool:
    """A shared host cannot widen its configured catalog/context selection."""
    source = SourceIdentity(
        adapter="entrysoft",
        source_code=provider["code"],
        dataset_code=record.get("dataset_code", "unknown"),
        language=record.get("language", "sv"),
    )
    binding, catalogs = catalog_configurations(source, record)
    return binding is None or bool(catalogs)


def _in_scope(provider: dict, hit: dict) -> bool:
    _, properties = _subject(hit, DCAT + "Distribution")
    urls = [o.get("value", "") for o in _values(properties, DCAT + "accessURL")]
    return any(
        within_catalog_scope(
            provider, {"dataset_code": TABLE_URL.match(url).group(1), "entry": hit}
        )
        for url in urls
        if TABLE_URL.match(url)
    )


async def _search_pass(client: Client, query: str) -> tuple[dict[str, dict], int]:
    """One pass over every page; returns the distinct hits and the number of pages."""
    hits, offset, pages = {}, 0, 0
    while True:
        pages += 1
        payload = await client.get(
            SEARCH_PATH,
            {"type": "solr", "query": query, "limit": SEARCH_PAGE, "offset": offset},
        )
        children = (payload.get("resource") or {}).get("children")
        if not isinstance(children, list):
            raise ValueError("Invalid EntryStore search response")
        for hit in children:
            hits[f"{hit.get('contextId')}/{hit.get('entryId')}"] = hit
        if len(children) < SEARCH_PAGE:
            return hits, pages  # a short page is the last one
        offset += SEARCH_PAGE


async def search_all(
    client: Client, query: str, *, max_passes: int = 4
) -> tuple[list[dict], int]:
    """Return every distinct hit, repeating passes until one adds nothing new.

    Returns the hits and the number of passes used.

    Raises:
        ValueError: If the result set still grows after ``max_passes`` passes.
    """
    hits: dict[str, dict] = {}
    for passes in range(1, max_passes + 1):
        found, pages = await _search_pass(client, query)
        new = found.keys() - hits.keys()
        hits.update(found)
        if passes == 1 and pages == 1:
            return list(hits.values()), passes  # one page: offsets cannot shift
        if passes > 1 and not new:
            return list(hits.values()), passes
    raise ValueError(f"Search results still changing after {max_passes} passes")


async def fetch_datasets(client: Client, uris: set[str]) -> dict[str, dict]:
    """Metadata of the given dataset resource URIs: batched search, then per-entry fallback."""
    datasets: dict[str, dict] = {}
    pending = sorted(uris)
    while pending:
        batch, size = [], 0
        while pending and size + len(pending[0]) < 3000:
            batch.append(pending.pop(0))
            size += len(batch[-1]) + 6
        query = "resource:(" + " OR ".join(f'"{uri}"' for uri in batch) + ")"
        payload = await client.get(
            SEARCH_PATH, {"type": "solr", "query": query, "limit": SEARCH_PAGE}
        )
        for hit in (payload.get("resource") or {}).get("children") or []:
            uri, properties = _subject(hit, DCAT + "Dataset")
            if uri in uris:
                datasets[uri] = properties
    for uri in sorted(uris - datasets.keys()):
        if "/resource/" not in uri:
            continue
        try:
            metadata = await client.get(uri.replace("/resource/", "/metadata/"))
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (401, 403, 404, 410):
                continue  # not public: the table is still listed, without dataset details
            raise
        if uri in metadata:
            datasets[uri] = metadata[uri]
    return datasets


def _subject(hit: dict, rdf_class: str) -> tuple[str, dict] | tuple[None, None]:
    for uri, properties in (hit.get("metadata") or {}).items():
        if any(o.get("value") == rdf_class for o in properties.get(RDF_TYPE, [])):
            return uri, properties
    return None, None


def _values(properties: dict, predicate: str) -> list[dict]:
    return properties.get(predicate, []) if properties else []


def _first(properties: dict, predicate: str) -> str | None:
    values = _values(properties, predicate)
    return values[0].get("value") if values else None


def _texts(properties: dict, predicate: str) -> dict[str, str]:
    """Literal values by language tag; untagged literals under ``""``."""
    texts: dict[str, str] = {}
    for item in _values(properties, predicate):
        texts.setdefault(item.get("lang", ""), item.get("value"))
    return texts


def _pick(texts: dict[str, str], language: str) -> str | None:
    for key in (language, "", "sv", "en"):
        if texts.get(key):
            return texts[key]
    return next(iter(texts.values()), None)


def _parent_dataset(hit: dict) -> str | None:
    for uri, properties in (hit.get("relations") or {}).items():
        if DCAT + "distribution" in properties:
            return uri
    return None


async def discover_provider(
    provider: dict, *, client: Client | None = None
) -> AsyncIterator[dict]:
    """Yield one scope per configured language; only complete scopes prove absence.

    ``provider``: ``{"code", "base_url", "languages"}``, e.g.
    ``{"code": "catalogalese", "base_url": "https://catalog.ale.se", "languages": ["sv"]}``.
    """
    if client is None:
        async with Client(provider["base_url"]) as owned:
            async for scope in discover_provider(provider, client=owned):
                yield scope
        return
    distributions = datasets = None
    error, passes = None, 0
    try:
        distributions, passes = await search_all(client, DISTRIBUTION_QUERY)
        distributions = [hit for hit in distributions if _in_scope(provider, hit)]
        referenced = {_parent_dataset(hit) for hit in distributions} - {None}
        datasets = await fetch_datasets(client, referenced)
    except (RateLimited, BudgetExhausted):
        raise  # the run's limits stop everything, not just this instance
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"

    for language in provider["languages"]:
        scope = {
            "language": language,
            "complete": False,
            "records": [],
            "observed_ids": [],
            "errors": [],
            "search_passes": passes,
        }
        if error:
            scope["errors"].append(error)
            yield scope
            continue
        by_table: dict[str, dict] = {}
        for hit in distributions:
            uri, properties = _subject(hit, DCAT + "Distribution")
            urls = [o.get("value", "") for o in _values(properties, DCAT + "accessURL")]
            tables = [url for url in urls if TABLE_URL.match(url)]
            if not uri or not tables:
                continue
            dataset_uri = _parent_dataset(hit)
            dataset = datasets.get(dataset_uri) or {}
            for table_url in tables:
                code = TABLE_URL.match(table_url).group(1)
                location = {"distribution": uri, "dataset": dataset_uri}
                if code in by_table:
                    by_table[code]["paths"].append(location)
                    continue
                distribution_modified = _first(properties, DCTERMS + "modified")
                dataset_modified = _first(dataset, DCTERMS + "modified")
                by_table[code] = {
                    "provider_code": provider["code"],
                    "dataset_code": code,
                    "language": language,
                    "base_url": provider["base_url"],
                    "table_url": table_url,
                    "title": _pick(_texts(dataset, DCTERMS + "title"), language)
                    or _pick(_texts(properties, DCTERMS + "title"), language),
                    "published": distribution_modified or dataset_modified,
                    "paths": [location],
                    "catalogue": {
                        "dataset_title": _texts(dataset, DCTERMS + "title"),
                        "dataset_description": _texts(dataset, DCTERMS + "description"),
                        "distribution_title": _texts(properties, DCTERMS + "title"),
                        "source_distribution": _first(properties, DCTERMS + "source"),
                        "distribution_modified": distribution_modified,
                        "dataset_modified": dataset_modified,
                        "dataset_issued": _first(dataset, DCTERMS + "issued"),
                        "publisher": _first(dataset, DCTERMS + "publisher"),
                        "themes": [
                            o.get("value") for o in _values(dataset, DCAT + "theme")
                        ],
                    },
                }
        scope["records"] = sorted(
            by_table.values(), key=lambda record: record["dataset_code"]
        )
        scope["observed_ids"] = [record["dataset_code"] for record in scope["records"]]
        scope["complete"] = True
        yield scope

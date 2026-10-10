"""Resolve organisations, catalog membership and concrete adapter-backed services."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote, unquote, urlsplit

from apps.configuration import load_configuration
from apps.model.models import Catalog, DataService, Reference, SourceIdentity


def _id(kind, *parts):
    return "urn:nordicintel:" + kind + ":" + ":".join(quote(p, safe="") for p in parts)


def inventory() -> list[dict]:
    """Compatibility view of transport sources, not a second registry."""
    configuration = load_configuration()
    result = []
    for binding in configuration.services.values():
        catalogs = [
            c for c in configuration.catalogs.values() if binding.id in c.services
        ]
        organisation = configuration.organisations.get(binding.identity_namespace)
        definition = configuration.adapters[binding.adapter]
        options = definition.defaults | binding.options
        result.append(
            {
                "adapter": binding.adapter,
                "source_code": binding.source_code,
                "owner": {
                    "code": binding.identity_namespace,
                    "label": next(iter(organisation.name.values()))[0],
                }
                if organisation
                else None,
                "owner_status": "configured" if organisation else "record_specific",
                "catalog_key": catalogs[0].key
                if len(catalogs) == 1
                else "shared-hosting",
                "catalog_label": next(iter(catalogs[0].title.values()))[0]
                if len(catalogs) == 1
                else urlsplit(binding.endpoint_url[0]).netloc,
                "software": next(iter(definition.title.values()))[0],
                "endpoint": binding.endpoint_url[0],
                "databases": options.get("database_ids", []),
                "service_id": binding.id,
                "identity_namespace": binding.identity_namespace,
                "evidence": ["adapters.json", "organisations.json", "catalogs.json"],
            }
        )
    return result


def deployment(adapter: str, source_code: str) -> dict:
    for item in inventory():
        if (item["adapter"], item["source_code"]) == (adapter, source_code):
            return item
    from apps.harvest.registry import get_adapter

    get_adapter(adapter)
    return {
        "adapter": adapter,
        "source_code": source_code,
        "owner": None,
        "owner_status": "record_specific",
        "catalog_key": source_code,
        "catalog_label": source_code,
        "endpoint": "",
        "databases": [],
        "identity_namespace": source_code,
    }


def concrete_service(binding, *, dataset_id: str | None = None) -> DataService:
    configuration = load_configuration()
    definition = configuration.adapters[binding.adapter]
    return DataService(
        id=binding.id,
        adapter=binding.adapter,
        title=binding.title or definition.title,
        description=definition.description,
        publisher=[configuration.organisations[binding.publisher]]
        if binding.publisher
        else [],
        endpoint_url=binding.endpoint_url,
        endpoint_description=binding.endpoint_description,
        serves_dataset=[Reference(id=dataset_id)] if dataset_id else [],
    )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def catalog_configurations(source: SourceIdentity, record: dict | list):
    """Resolve membership from configured identity/context, never host alone."""
    configuration = load_configuration()
    binding = next(
        (
            b
            for b in configuration.services.values()
            if (b.adapter, b.source_code) == (source.adapter, source.source_code)
        ),
        None,
    )
    if binding is None:
        return None, []
    strings = list(_strings(record))
    selected = []
    for catalog in configuration.catalogs.values():
        if binding.id not in catalog.services:
            continue
        scope = catalog.scopes.get(binding.id)
        if scope and (scope.contexts or scope.dataset_codes):
            if not (
                source.dataset_code in scope.dataset_codes
                or any(s in scope.dataset_codes for s in strings)
                or any(
                    value.startswith(context)
                    for context in scope.contexts
                    for value in strings
                )
            ):
                continue
        selected.append(catalog)
    return binding, selected


def resolve_source(
    source: SourceIdentity,
    data_url: str | None,
    record: dict | list,
    *,
    dataset_id: str | None = None,
) -> tuple[list[Catalog], DataService]:
    configuration = load_configuration()
    binding, selected = catalog_configurations(source, record)
    service = (
        concrete_service(binding, dataset_id=dataset_id)
        if binding
        else service_for_url(source.adapter, data_url)
    )
    catalogs = []
    for item in selected:
        database = record.get("database_id") if isinstance(record, dict) else None
        if source.adapter == "riksbank" and data_url:
            match = re.match(
                r"^https://api.riksbank.se/(swea|monetary_policy_data)/v1(?:/|$)",
                data_url,
            )
            database = match[1] if match else database
        identity, title = item.id, item.title
        if database and str(database) != item.key:
            identity += ":" + quote(str(database), safe="")
            title = {
                lang: [text + ": " + str(database) for text in texts]
                for lang, texts in title.items()
            }
        catalogs.append(
            Catalog(
                id=identity,
                title=title,
                description=item.description,
                homepage=item.homepage,
                publisher=[configuration.organisations[item.publisher]]
                if item.publisher
                else [],
                languages=[
                    Reference(
                        id="http://publications.europa.eu/resource/authority/language/"
                        + {"sv": "SWE", "en": "ENG"}.get(lang, lang)
                    )
                    for lang in item.languages
                ],
                services=[Reference(id=s) for s in item.services],
                catalogs=[Reference(id=c) for c in item.catalogs],
                has_part=[Reference(id=c) for c in item.has_part],
            )
        )
    return catalogs, service


def source_entities(source: SourceIdentity, data_url: str | None, record: dict):
    """Transition helper for callers expecting one catalog."""
    catalogs, service = resolve_source(source, data_url, record)
    if len(catalogs) > 1:
        raise ValueError("Source belongs to multiple catalogs; use resolve_source")
    return (catalogs[0] if catalogs else None), service


def service_for_url(adapter: str, url: str | None) -> DataService:
    """Find the concrete service matching a URL without borrowing a publisher."""
    configuration = load_configuration()
    candidates = []
    for binding in configuration.services.values():
        if binding.adapter != adapter:
            continue
        for endpoint in binding.endpoint_url:
            actual, configured = urlsplit(url or ""), urlsplit(endpoint)
            actual_path = unquote(actual.path).lower().rstrip("/")
            configured_path = unquote(configured.path).lower().rstrip("/")
            if (
                actual.hostname == configured.hostname
                and actual.port == configured.port
                and (
                    actual_path == configured_path
                    or actual_path.startswith(configured_path + "/")
                )
            ):
                candidates.append((len(endpoint), binding))
    if candidates:
        return concrete_service(max(candidates, key=lambda item: item[0])[1])
    definition = configuration.adapters[adapter]
    endpoint = url
    if url:
        match = re.match(r"^(https?://[^/]+/.*?api/v[12])(?:/|$)", url, flags=re.I)
        if match:
            endpoint = match[1]
    suffix = hashlib.sha256((endpoint or "missing-endpoint").encode()).hexdigest()[:20]
    return DataService(
        id=_id("service", "external", adapter, suffix),
        adapter=adapter,
        title=definition.title,
        description=definition.description,
        endpoint_url=[endpoint] if endpoint else [],
    )

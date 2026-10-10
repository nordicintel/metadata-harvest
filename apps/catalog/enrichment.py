"""Enrich harvested metadata while retaining source assertions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from urllib.parse import urlsplit

from apps.catalog.filtering import is_scb_v1_url
from apps.catalog.matching import PortalIndex, _list, _strings, _url
from apps.harvest.sources import inventory, service_for_url
from apps.model.models import (
    Agent,
    Concept,
    ContactPoint,
    Dataset,
    Distribution,
    VocabularyResource,
)
from apps.model.normalization import stable_id


def _texts(value):
    if isinstance(value, str):
        return {"und": [value]}
    if isinstance(value, dict):
        return {
            language: values if isinstance(values, list) else [values]
            for language, values in value.items()
            if isinstance(values, (str, list))
        }
    return {}


def _resource(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("resource") or value.get("id")
    return None


def _urls(value):
    return [
        resource
        for item in _list(value)
        if isinstance(resource := _resource(item), str) and _url(resource)
    ]


def _link_attributes(model, key, value):
    """Retain assertions attached to URL objects without duplicating mapped URLs."""
    for position, item in enumerate(_list(value)):
        if isinstance(item, dict):
            chosen = "resource" if item.get("resource") else "id"
            extra = {k: v for k, v in item.items() if k != chosen}
            if extra:
                model.source_assertions.setdefault(key + "Attributes", []).append(
                    {
                        "position": position,
                        "resource": _resource(item),
                        "attributes": extra,
                    }
                )
        if item is not None and (
            not isinstance(_resource(item), str) or not _url(_resource(item))
        ):
            model.source_assertions.setdefault("unmappedLinks", {}).setdefault(
                key, []
            ).append(item)


def _adapter_for_url(url):
    if is_scb_v1_url(url):
        return "pxweb_v1"
    if "/rowstore/" in url:
        return "entrysoft"
    if "api.kolada.se/" in url:
        return "kolada"
    if "statistikdatabasen.scb.se/api/v2" in url:
        return "pxweb_v2"
    parsed = urlsplit(url)
    for item in inventory():
        endpoint = urlsplit(item["endpoint"])
        if parsed.hostname == endpoint.hostname and parsed.path.casefold().startswith(
            endpoint.path.rstrip("/").casefold() + "/"
        ):
            return item["adapter"]
    return None


def _vocabulary(value):
    result = []
    for item in _list(value):
        if isinstance(item, str):
            result.append(
                VocabularyResource(id=item)
                if _url(item)
                else VocabularyResource(label={"und": [item]})
            )
        elif isinstance(item, dict):
            rest = deepcopy(item)
            resource = rest.pop("resource", None)
            label = _texts(
                rest.pop("label") if "label" in rest else rest.pop("title", {})
            )
            identifier = rest.pop("id", None)
            result.append(
                VocabularyResource(
                    id=resource,
                    label=label,
                    identifiers=[identifier]
                    if isinstance(identifier, str) and identifier != resource
                    else [],
                    additional_attributes={"data_eu": rest} if rest else {},
                )
            )
        else:
            raise ValueError("Malformed linked resource")
    return result


def _agent(value):
    rest = deepcopy(value)
    result = Agent(
        id=rest.pop("resource", None),
        name=_texts(rest.pop("name", {})),
        homepage=_urls(rest.pop("homepage", [])),
        email=rest.pop("email", None),
    )
    if rest:
        result.additional_attributes["data_eu"] = rest
    return result


def _portal_fields(model, source):
    """Map standard semantics; malformed optional source values remain with findings."""
    fields = {
        "language": "languages",
        "conforms_to": "conforms_to",
        "type": "resource_types",
        "access_right": "access_rights",
        "rights": "rights",
        "applicable_legislation": "applicable_legislation",
        "status": "status",
    }
    if isinstance(model, Dataset):
        fields.update(
            accrual_periodicity="frequency",
            spatial_resource="spatial",
            provenance="provenance",
            relation="related",
            source="source",
            in_series="in_series",
            is_referenced_by="referenced_by",
        )
    else:
        fields.update(
            format="format", media_type="media_type", availability="availability"
        )
    transforms = {key: (dest, _vocabulary) for key, dest in fields.items()}
    transforms.update(
        issued=("issued", lambda v: v),
        spatial_resolution_in_meters=("spatial_resolution", lambda v: _list(v)),
    )
    if isinstance(model, Dataset):
        transforms.update(
            creator=("creators", lambda v: [_agent(x) for x in _list(v)]),
            version_info=("version", lambda v: v),
            version_notes=("version_notes", _texts),
        )
    else:
        transforms.update(
            modified=("modified", lambda v: v),
            byte_size=("byte_size", lambda v: v),
            temporal_resolution=("temporal_resolution", _list),
        )
    for key, (field, transform) in transforms.items():
        if key in source:
            try:
                setattr(model, field, transform(source[key]))
            except (ValueError, TypeError, AttributeError) as error:
                model.source_assertions.setdefault("diagnostics", {})[key] = str(error)
            else:
                source.pop(key)
    if isinstance(model, Dataset):
        for keyword in source.pop("keywords", []):
            if isinstance(keyword, dict) and isinstance(keyword.get("label"), str):
                language = keyword.get("language") or "und"
                model.keywords.setdefault(language, []).append(keyword["label"])
                extra = {
                    k: v for k, v in keyword.items() if k not in {"label", "language"}
                }
                if extra:
                    model.source_assertions.setdefault("keywordIdentifiers", []).append(
                        extra
                    )
            else:
                model.source_assertions.setdefault("malformedKeywords", []).append(
                    keyword
                )
        contacts = source.pop("contact_point", [])
        for contact in _list(contacts):
            try:
                rest = deepcopy(contact)
                item = ContactPoint(
                    name=rest.pop("name", None),
                    email=rest.pop("email", None),
                    telephone=rest.pop("telephone", None),
                    url=rest.pop("url", None),
                    additional_attributes={"data_eu": rest},
                )
                model.contacts.append(item)
            except (ValueError, TypeError, AttributeError):
                model.source_assertions.setdefault("malformedContacts", []).append(
                    contact
                )
        # Harvested coverage and modification remain authoritative. Keep portal
        # assertions distinct from data values and from catalog-record timestamps.
        for old, prop in (
            ("modified", "dct:modified"),
            ("temporal", "dct:temporal"),
            ("temporal_resolution", "dcat:temporalResolution"),
        ):
            if old in source:
                model.source_assertions[prop] = source.pop(old)


def enrich(dataset: Dataset, index: PortalIndex) -> Dataset:
    result = dataset.model_copy(deep=True)
    match = index.match(dataset)
    result.match_evidence.append(
        {
            "status": match.status,
            "method": match.method,
            "record_id": match.record_id,
            "candidates": list(match.candidates),
            "relation": match.relation,
        }
    )
    if match.status != "matched":
        return result
    source = deepcopy(index.documents[match.record_id])
    result.match_evidence[-1]["resource"] = source.get("resource")
    result.match_evidence[-1]["sha256"] = hashlib.sha256(
        json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if match.relation != "equivalent":
        return result
    conflicts = {}
    for field in ("title", "description"):
        incoming = _texts(source.pop(field, {}))
        target = getattr(result, field)
        for language, values in incoming.items():
            if language not in target:
                target[language] = values
            elif target[language] != values:
                conflicts.setdefault(field, {})[language] = values
    publisher = source.pop("publisher", None)
    if isinstance(publisher, dict):
        result.publisher = [_agent(publisher)]
    elif publisher is not None:
        source["publisher"] = publisher
    _portal_fields(result, source)
    for key, target in (("landing_page", "landing_page"), ("page", "documentation")):
        value = source.pop(key, [])
        _link_attributes(result, key, value)
        setattr(
            result,
            target,
            list(dict.fromkeys(getattr(result, target) + _urls(value))),
        )
    for category in source.pop("categories", []):
        identifier = _resource(category)
        if identifier:
            result.themes.append(
                Concept(
                    id=identifier,
                    code=category.get("id", identifier)
                    if isinstance(category, dict)
                    else identifier,
                    label=_texts(category.get("label", {}))
                    if isinstance(category, dict)
                    else {},
                    additional_attributes={
                        "data_eu": {
                            k: v
                            for k, v in category.items()
                            if k not in {"resource", "id", "label"}
                        }
                    }
                    if isinstance(category, dict)
                    else {},
                )
            )
        else:
            result.source_assertions.setdefault("unmappedCategories", []).append(
                category
            )
    for distribution in source.pop("distributions", []):
        access = _urls(distribution.get("access_url", []))
        downloads = _urls(distribution.get("download_url", []))
        query_endpoints = [
            url
            for url in downloads
            if _adapter_for_url(url) == "pxweb_v1"
            and urlsplit(url).path.rstrip("/").lower().endswith(".px")
        ]
        access = list(dict.fromkeys(access + query_endpoints))
        downloads = [url for url in downloads if url not in query_endpoints]
        licenses = distribution.get("license", [])
        if not isinstance(licenses, list):
            licenses = [licenses]
        license_ids = [v for x in licenses if (v := _resource(x))]
        services = []
        for url in access + downloads:
            adapter = _adapter_for_url(url)
            if adapter and all(s.adapter != adapter for s in services):
                services.append(service_for_url(adapter, url))
        dist = Distribution(
            id=stable_id(
                "distribution",
                "data_eu",
                source["id"],
                distribution.get("id")
                or hashlib.sha256(
                    json.dumps(distribution, sort_keys=True).encode()
                ).hexdigest(),
            ),
            title=_texts(distribution.get("title", {})),
            description=_texts(distribution.get("description", {})),
            access_url=access,
            download_url=downloads,
            license=license_ids,
            services=services,
            additional_attributes={
                "data_eu": {
                    k: v
                    for k, v in distribution.items()
                    if k
                    not in {
                        "id",
                        "title",
                        "description",
                        "access_url",
                        "download_url",
                        "license",
                    }
                }
            },
        )
        remaining = dist.additional_attributes.get("data_eu", {})
        if query_endpoints:
            dist.source_assertions["downloadURLsMappedToAccess"] = query_endpoints
        for key in ("access_url", "download_url"):
            _link_attributes(dist, key, distribution.get(key, []))
        for position, value in enumerate(licenses):
            if isinstance(value, dict):
                extra = {k: v for k, v in value.items() if k not in {"resource", "id"}}
                if extra:
                    dist.source_assertions.setdefault("licenseAttributes", []).append(
                        {
                            "position": position,
                            "resource": _resource(value),
                            "attributes": extra,
                        }
                    )
            if not _resource(value):
                dist.source_assertions.setdefault("unmappedLicenses", []).append(value)
        _portal_fields(dist, remaining)
        for key, field in (("page", "documentation"),):
            if key in remaining:
                value = remaining.pop(key)
                _link_attributes(dist, key, value)
                setattr(dist, field, _urls(value))
        # Services stay the shared adapter nodes. Portal service assertions are evidence.
        if "access_service" in remaining:
            dist.source_assertions["portalAccessServices"] = remaining.pop(
                "access_service"
            )
        result.distributions.append(dist)
    source.pop("id", None)
    result.identifiers = sorted(
        set(result.identifiers + list(_strings(source.pop("identifier", []))))
    )
    source.pop("resource", None)
    if conflicts:
        source["conflicts"] = conflicts
    result.additional_attributes["data_eu"] = source
    return result

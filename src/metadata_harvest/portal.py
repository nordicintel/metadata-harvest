"""Accepted data.eu snapshots and deterministic, evidence-based enrichment."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from metadata_harvest.models import (
    Agent,
    Concept,
    ContactPoint,
    Dataset,
    Distribution,
    Reference,
    VocabularyResource,
)
from metadata_harvest.sources import inventory, service_for_url
from metadata_harvest.statdcat import stable_id

SEARCH_URL = "https://data.europa.eu/api/hub/search"


def records(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                raise ValueError(f"Truncated JSONL line {number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Invalid record at line {number}")
            yield value


def import_snapshot(
    source: Path, target: Path, *, expected_count: int | None = None
) -> dict:
    """Accept a complete snapshot; invalid imports never replace accepted bytes."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="portal-", dir=target.parent) as temporary:
        candidate = Path(temporary) / "oppnadata.jsonl.gz"
        seen = set()
        with (
            candidate.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out,
        ):
            for record in records(Path(source)):
                identifier = record.get("id")
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier in seen
                ):
                    raise ValueError("Missing or duplicate portal identity")
                if record.get("catalog", {}).get("id") != "oppnadata":
                    raise ValueError("Portal record belongs to another catalog")
                seen.add(identifier)
                out.write(
                    (
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode()
                )
        if not seen or expected_count is not None and len(seen) != expected_count:
            raise ValueError("Incomplete portal snapshot")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        report = {
            "count": len(seen),
            "sha256": digest,
            "catalog": "oppnadata",
            "query": {"filters": "dataset", "facets": {"catalog": ["oppnadata"]}},
            "coverage": {
                "complete": True,
                "expected_count": expected_count,
                "identifiers_sha256": hashlib.sha256(
                    "\n".join(sorted(seen)).encode()
                ).hexdigest(),
            },
            "source": SEARCH_URL,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "status": "success",
        }
        target.mkdir(parents=True, exist_ok=True)
        current = target / candidate.name
        if not current.exists() or current.read_bytes() != candidate.read_bytes():
            candidate.replace(current)
            state = target / "state.json"
            stage = target / ".state.json.tmp"
            stage.write_text(
                json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            stage.replace(state)
        elif (target / "state.json").exists():
            old = json.loads((target / "state.json").read_text(encoding="utf-8"))
            report["retrieved_at"] = old.get("retrieved_at", report["retrieved_at"])
            if report != old:
                stage = target / ".state.json.tmp"
                stage.write_text(
                    json.dumps(report, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                stage.replace(target / "state.json")
        return report


def accepted_records(target: Path):
    """Verify the accepted snapshot's manifest, scope and full record count offline."""
    path = target / "oppnadata.jsonl.gz"
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != state["sha256"]:
            raise ValueError("Portal snapshot checksum mismatch")
    count = 0
    for record in records(path):
        if record.get("catalog", {}).get("id") != "oppnadata":
            raise ValueError("Invalid accepted portal scope")
        count += 1
        yield record
    if count != state["count"]:
        raise ValueError("Incomplete accepted portal coverage")


def refresh_snapshot(target: Path, *, client=None) -> dict:
    """Fetch a complete search scroll before exposing it to offline builders."""
    own = client is None
    client = client or httpx.Client(timeout=120, follow_redirects=True)
    try:
        response = client.get(
            SEARCH_URL + "/search",
            params={
                "filters": "dataset",
                "limit": 200,
                "scroll": "true",
                "facets": json.dumps({"catalog": ["oppnadata"]}),
            },
        )
        response.raise_for_status()
        result = response.json()["result"]
        expected = result["count"]
        scroll = result.get("scrollId")
        batch = result.get("results", [])
        with TemporaryDirectory(prefix="portal-download-") as temporary:
            path = Path(temporary) / "snapshot.jsonl"
            count = 0
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                while batch:
                    for record in batch:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += len(batch)
                    if count >= expected:
                        break
                    if not scroll:
                        raise ValueError("Missing portal scroll cursor")
                    response = client.get(
                        SEARCH_URL + "/scroll", params={"scrollId": scroll}
                    )
                    response.raise_for_status()
                    result = response.json()["result"]
                    scroll = result.get("scrollId", scroll)
                    batch = result.get("results", [])
            return import_snapshot(path, target, expected_count=expected)
    finally:
        if own:
            client.close()


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        authority = parsed.netloc.lower()
        if (parsed.scheme, parsed.port) in {("https", 443), ("http", 80)}:
            authority = authority.rsplit(":", 1)[0]
        return urlunsplit(
            (
                parsed.scheme.lower(),
                authority,
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    except ValueError:
        return None


def _normal(value):
    return re.sub(r"\W+", "", value.casefold())


def _identifier_key(value):
    """Comparison only: normalize URL encoding, lowercase, strip punctuation/space."""
    return re.sub(r"[\W_]+", "", unquote(value).lower())


@dataclass(frozen=True)
class Match:
    status: str
    record_id: str | None = None
    candidates: tuple[str, ...] = ()
    method: str | None = None
    relation: str = "equivalent"


class PortalIndex:
    """Index identifiers by owner; shared URL values never establish identity."""

    def __init__(self, documents, *, overrides=None):
        self.documents = {}
        self.overrides = overrides or {}
        self.identifiers = defaultdict(set)
        self.urls = defaultdict(set)
        self.pxweb_sources = [
            (urlsplit(entry["endpoint"]), entry["owner"]["code"])
            for entry in inventory()
            if entry["adapter"] == "pxweb_v1" and entry["owner"]
        ]
        self.service_urls = {
            url.rstrip("/") for entry in inventory() if (url := _url(entry["endpoint"]))
        }
        names = defaultdict(set)
        for entry in inventory():
            if entry["owner"]:
                names[_normal(entry["owner"]["label"])].add(entry["owner"]["code"])
        names["statisticssweden"].add("scb")
        names["kolada"].add("rka")
        for document in documents:
            identifier = document["id"]
            if identifier in self.documents:
                raise ValueError("Duplicate portal record identity")
            self.documents[identifier] = document
            publisher = document.get("publisher", {})
            publisher_text = [_normal(s) for s in _strings(publisher.get("name", ""))]
            owners = set()
            for text in publisher_text:
                for name, codes in names.items():
                    if (
                        text == name
                        or name
                        in {"kolada", "statistiskacentralbyrån", "statisticssweden"}
                        and name in text
                    ):
                        owners.update(codes)
            for owner in owners:
                for value in _strings(document.get("identifier", [])):
                    if key := _identifier_key(value):
                        self.identifiers[(owner, key)].add(identifier)
            for key in ("identifier", "landing_page", "resource"):
                for value in _strings(document.get(key, [])):
                    if url := _url(value):
                        self.urls[url].add(identifier)
            for distribution in document.get("distributions", []):
                for service in _list(distribution.get("access_service", [])):
                    if isinstance(service, dict):
                        for value in _strings(service.get("endpoint_url", [])):
                            if url := _url(value):
                                self.service_urls.add(url.rstrip("/"))
                for key in ("access_url", "download_url"):
                    for value in _strings(distribution.get(key, [])):
                        if url := _url(value):
                            self.urls[url].add(identifier)
                            for owner, native_id in self.native_pxweb_ids(url):
                                if key := _identifier_key(native_id):
                                    self.identifiers[(owner, key)].add(identifier)

    def native_pxweb_ids(self, url):
        """Read opaque table IDs only from configured provider API paths."""
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/")
        if not path.lower().endswith(".px"):
            return
        for endpoint, owner in self.pxweb_sources:
            prefix = endpoint.path.rstrip("/") + "/"
            if parsed.hostname != endpoint.hostname or not path.casefold().startswith(
                prefix.casefold()
            ):
                continue
            # Language, database and table are required; a service root is not identity.
            relative = path[len(prefix) :].split("/")
            if len(relative) >= 3:
                yield owner, unquote(relative[-1])

    def probe(self, dataset: Dataset) -> dict:
        urls = list(dataset.landing_page)
        for distribution in dataset.distributions:
            urls.extend(distribution.access_url)
            if distribution.retrieval and distribution.retrieval.metadata_url:
                urls.append(distribution.retrieval.metadata_url)
        return {
            "dataset_id": dataset.id,
            "owners": sorted(
                {
                    a.id.rsplit(":", 1)[-1]
                    for c in dataset.catalogs
                    for a in c.publisher
                    if a.id
                }
            ),
            "identifiers": sorted(dataset.identifiers),
            "urls": sorted(set(urls)),
        }

    def fingerprint(self, probes: list[dict]) -> str:
        assertions = []
        for probe in probes:
            match = self.match_probe(probe)
            ids = match.candidates or ((match.record_id,) if match.record_id else ())
            assertions.append(
                {
                    "probe": probe,
                    "status": match.status,
                    "method": match.method,
                    "relation": match.relation,
                    "records": [self.documents[i] for i in ids],
                }
            )
        return hashlib.sha256(
            json.dumps(
                assertions, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()

    def match(self, dataset: Dataset) -> Match:
        return self.match_probe(self.probe(dataset))

    def match_probe(self, probe: dict) -> Match:
        override = self.overrides.get(probe["dataset_id"])
        if override:
            record_id = override["record_id"]
            if record_id not in self.documents:
                raise ValueError(f"Missing override target: {record_id}")
            relation = override.get("relation", "equivalent")
            if relation not in {"equivalent", "broader"}:
                raise ValueError("Unsupported match relation")
            return Match("matched", record_id, method="override", relation=relation)
        owners = probe["owners"]
        candidates = set()
        for owner in owners:
            for identifier in probe["identifiers"]:
                candidates.update(
                    self.identifiers.get((owner, _identifier_key(identifier)), ())
                )
        if candidates:
            return (
                Match("matched", next(iter(candidates)), method="identifier")
                if len(candidates) == 1
                else Match(
                    "ambiguous",
                    candidates=tuple(sorted(candidates)),
                    method="identifier",
                )
            )
        urls = probe["urls"]
        for value in urls:
            url = _url(value)
            if not url or not self.dataset_specific_url(url):
                continue
            found = self.urls.get(url, set())
            if len(found) == 1:
                candidates.update(found)
        if len(candidates) == 1:
            return Match("matched", next(iter(candidates)), method="url")
        return Match(
            "ambiguous" if candidates else "unmatched",
            candidates=tuple(sorted(candidates)),
            method="url",
        )

    def dataset_specific_url(self, url: str) -> bool:
        """A singleton service URL is still a service, never dataset evidence."""
        if url.rstrip("/") in self.service_urls:
            return False
        parsed = urlsplit(url)
        parts = [part.casefold() for part in parsed.path.split("/") if part]
        generic = {
            "api",
            "data",
            "datasets",
            "tables",
            "statistics",
            "statistik",
            "rowstore",
            "doris",
            "ssd",
            "sv",
            "en",
            "v1",
            "v2",
            "v3",
            "ov0104",
        }
        return bool(parts) and any(part not in generic for part in parts)


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


def is_scb_v1_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in {
        "api.scb.se",
        "www.statistikdatabasen.scb.se",
        "statistikdatabasen.scb.se",
    } and ("/OV0104/v1/" in parsed.path or "/api/v1/" in parsed.path.lower())


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


def _list(value):
    return value if isinstance(value, list) else [value]


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


def publication_view(dataset: Dataset, *, include_scb_pxweb_v1=False) -> Dataset:
    """Filter a copy; imported/accepted descriptions remain available for rebuilding."""
    result = dataset.model_copy(deep=True)
    if not include_scb_pxweb_v1:
        result.distributions = [
            d
            for d in result.distributions
            if not any(is_scb_v1_url(u) for u in d.access_url + d.download_url)
        ]
        structures = {
            d.structure.id: d.structure for d in result.distributions if d.structure
        }
        if (
            isinstance(result.structure, Reference)
            and result.structure.id not in structures
        ):
            result.structure = None
        if result.structure is not None and not isinstance(result.structure, Reference):
            structures[result.structure.id] = result.structure
        dimensions = {c.dimension.id for s in structures.values() for c in s.components}
        result.dimensions = [
            d
            for d in result.dimensions
            if not isinstance(d, Reference) or d.id in dimensions
        ]
        used_services = {s.id for d in result.distributions for s in d.services}
        for catalog in result.catalogs:
            catalog.services = [s for s in catalog.services if s.id in used_services]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["import", "refresh", "publish"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/data_eu"))
    args = parser.parse_args()
    if args.command == "import":
        if args.source is None:
            parser.error("--source is required for import")
        result = import_snapshot(args.source, args.output)
    elif args.command == "refresh":
        result = refresh_snapshot(args.output)
    else:
        from metadata_harvest.publication import publish

        publish("Refresh oppnadata editorial snapshot", data=args.output)
        result = {"status": "published"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()

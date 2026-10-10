"""Conservative evidence-based matching against accepted portal records."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from apps.harvest.sources import inventory
from apps.model.models import (
    Dataset,
)

logger = logging.getLogger(__name__)


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
        started = time.perf_counter()
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
        logger.info(
            "portal index: %d records, %d identifier keys, %d url keys, "
            "%d overrides in %.1fs",
            len(self.documents),
            len(self.identifiers),
            len(self.urls),
            len(self.overrides),
            time.perf_counter() - started,
        )

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
        started = time.perf_counter()
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
        digest = hashlib.sha256(
            json.dumps(
                assertions, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        logger.debug(
            "portal fingerprint over %d probes in %.2fs",
            len(probes),
            time.perf_counter() - started,
        )
        return digest

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


def _list(value):
    return value if isinstance(value, list) else [value]

"""PXWeb v2 fixtures: controlled HTTP responses and a run helper, no SCB traffic."""

import asyncio

import httpx

from tests.support.binding import binding

PROVIDER = binding(
    "pxweb_v2",
    "scb",
    "https://example.test/api/v2",
    languages=["sv", "en"],
    page_size=2,
)
CONFIG = {
    "languages": [{"id": "sv"}, {"id": "en"}],
    "maxCallsPerTimeWindow": 30,
    "timeWindow": 10,
}


def table(code="T1"):
    return {
        "id": code,
        "label": "Catalogue title",
        "updated": "2026-10-01T06:00:00Z",
        "firstPeriod": "2020",
        "lastPeriod": "2026",
        "paths": [],
        "links": [],
        "category": "public",
        "variableNames": ["region"],
        "source": "SCB",
    }


def record(code="T1", language="sv"):
    return {
        "provider_code": "scb",
        "dataset_code": code,
        "language": language,
        "base_api_url": PROVIDER.endpoint_url,
        "catalogue": table(code),
    }


def metadata(code="T1", language="sv"):
    return {
        "class": "dataset",
        "version": "2.0",
        "label": "Native metadata title",
        "updated": "2026-10-01T06:00:00Z",
        "id": ["Region"],
        "size": [2],
        "dimension": {
            "Region": {
                "label": "region",
                "category": {
                    "index": {"01": 0, "02": 1},
                    "label": {"01": "One", "02": "Two"},
                },
                "extension": {"codelists": [{"id": "vs_region", "type": "Valueset"}]},
            }
        },
        "extension": {"px": {"tableid": code, "language": language}, "contact": []},
        "value": [],
        "note": ["Source note"],
    }


def codelist(language="sv"):
    return {
        "id": "vs_region",
        "language": language,
        "languages": ["sv", "en"],
        "label": "Regions",
        "type": "Valueset",
        "links": [],
        "values": [{"code": "01", "label": "One", "valueMap": ["01"]}],
    }


class Time:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    async def sleep(self, delay):
        self.value += delay


def client_for(handler):
    from apps.harvest.metadata_adapters.pxweb_v2.client import Client

    clock = Time()
    return Client(
        PROVIDER.endpoint_url,
        transport=httpx.MockTransport(handler),
        clock=clock.now,
        sleep=clock.sleep,
    ), clock


class Source:
    """An SCB answering the catalogue, metadata and codelist endpoints."""

    def __init__(self):
        self.tables = {"sv": [table()], "en": [table("EN1")]}
        self.requests = []
        self.fail_languages = set()
        self.fail_metadata = set()
        self.fail_codes = set()
        self.updated = None
        self.label = "Native metadata title"

    def handle(self, request):
        path, params = request.url.path, request.url.params
        language = params.get("lang")
        self.requests.append((path, language))
        if path.endswith("/config"):
            return httpx.Response(200, json=CONFIG)
        if path.endswith("/tables"):
            if language in self.fail_languages:
                return httpx.Response(503)
            rows = self.tables[language]
            return httpx.Response(
                200,
                json={
                    "language": language,
                    "tables": rows,
                    "page": {
                        "pageNumber": 1,
                        "pageSize": 1000,
                        "totalElements": len(rows),
                        "totalPages": 1,
                    },
                },
            )
        if "/codelists/" in path:
            return (
                httpx.Response(503)
                if language in self.fail_codes
                else httpx.Response(200, json=codelist(language))
            )
        code = path.split("/")[-2]
        if code in self.fail_metadata:
            return httpx.Response(503)
        payload = metadata(code, language)
        payload["label"] = self.label
        payload["updated"] = self.updated or payload["updated"]
        return httpx.Response(200, json=payload)


def harvest(source, root, output, **selection):
    """Run the adapter against ``source`` and collect the output into ``root``."""
    from apps.harvest.metadata_adapters.pxweb_v2.harvest import ADAPTER
    from harvest import lifecycle

    async def execute():
        client, _ = client_for(source.handle)
        async with client:
            return await lifecycle.run(
                ADAPTER, PROVIDER, root, output, selection=selection, client=client
            )

    result = asyncio.run(execute())
    lifecycle.collect(ADAPTER, root, output)
    return result

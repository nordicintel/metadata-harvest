"""Exercise the v2 boundary against controlled HTTP responses, without SCB traffic."""

import asyncio
from copy import deepcopy

import httpx
import pytest

PROVIDER = {
    "code": "scb",
    "base_api_url": "https://example.test/api/v2",
    "languages": ["sv", "en"],
    "page_size": 2,
}
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
        "base_api_url": PROVIDER["base_api_url"],
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
    from harvesting.adapters.pxweb_v2.client import Client

    clock = Time()
    return Client(
        PROVIDER["base_api_url"],
        transport=httpx.MockTransport(handler),
        clock=clock.now,
        sleep=clock.sleep,
    ), clock


def test_client_paces_requests_and_retries_under_stricter_config():
    from harvesting.adapters.pxweb_v2.client import Client

    clock, starts = Time(), []

    def handler(request):
        starts.append(clock.now())
        if len(starts) == 2:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=CONFIG)

    async def run():
        async with Client(
            PROVIDER["base_api_url"],
            transport=httpx.MockTransport(handler),
            clock=clock.now,
            sleep=clock.sleep,
        ) as client:
            await client.get("config")
            client.configure({**CONFIG, "maxCallsPerTimeWindow": 2, "timeWindow": 10})
            await client.get("config")

    asyncio.run(run())
    assert starts == [0, 5, 10]


@pytest.mark.parametrize(
    "failure", [None, "duplicate", "missing", "totals", "language", "malformed"]
)
def test_discovery_proves_complete_pagination_per_language(failure):
    from harvesting.adapters.pxweb_v2.discovery import discover_provider

    def handler(request):
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json=CONFIG)
        params = request.url.params
        assert params["includeDiscontinued"] == "true"
        number, language = int(params["pageNumber"]), params["lang"]
        rows = [table("T1"), table("T2")] if number == 1 else [table("T3")]
        total = 3 if language == "sv" else 1
        if language == "en":
            rows = [table("EN1")]
        if language == "sv" and number == 2:
            if failure == "missing":
                return httpx.Response(503)
            if failure == "duplicate":
                rows = [table("T1")]
            if failure == "totals":
                total = 4
            if failure == "malformed":
                rows = [{"id": "T3"}]
        body = {
            "language": "wrong"
            if failure == "language" and language == "sv"
            else language,
            "tables": rows,
            "page": {
                "pageNumber": number,
                "pageSize": 2,
                "totalElements": total,
                "totalPages": 2 if language == "sv" else 1,
            },
        }
        return httpx.Response(200, json=body)

    async def run():
        client, _ = client_for(handler)
        async with client:
            return [s async for s in discover_provider(PROVIDER, client=client)]

    scopes = asyncio.run(run())
    assert scopes[0]["complete"] is (failure is None)
    assert scopes[1]["complete"] is True
    assert [r["dataset_code"] for r in scopes[1]["records"]] == ["EN1"]
    if failure is None:
        assert len(scopes[0]["records"]) == 3
        assert scopes[0]["records"][0]["catalogue"] == table("T1")


def test_metadata_preserves_native_payload_and_fetches_encoded_codelist():
    from harvesting.adapters.pxweb_v2.metadata import (
        fetch_codelist,
        fetch_metadata,
    )

    def handler(request):
        if "/codelists/" in request.url.path:
            return httpx.Response(200, json=codelist("en"))
        assert request.url.params["defaultSelection"] == "false"
        assert request.url.params["lang"] == "en"
        return httpx.Response(200, json=metadata(language="en"))

    async def run():
        client, _ = client_for(handler)
        async with client:
            return (
                await fetch_metadata(record(language="en"), client=client),
                await fetch_codelist("vs_region", "en", client=client),
            )

    document, codes = asyncio.run(run())
    saved = document["extension"]["pxweb_v2"]["source_record"]
    assert (
        document["dimension"]["Region"]["category"]
        == metadata(language="en")["dimension"]["Region"]["category"]
    )
    assert document["label"] == metadata(language="en")["label"]
    assert document["value"] == []
    assert document["extension"]["nordicintel"]["language"] == "en"
    assert saved["catalogue"] == table()
    assert codes == codelist("en")


@pytest.mark.parametrize(
    "defect", ["values", "language", "table", "counts", "index", "labels"]
)
def test_invalid_metadata_is_rejected(defect):
    from harvesting.adapters.pxweb_v2.validation import validate_metadata

    value = metadata()
    if defect == "values":
        value["value"] = [1]
    elif defect in {"language", "table"}:
        value["extension"]["px"]["language" if defect == "language" else "tableid"] = (
            "wrong"
        )
    elif defect == "counts":
        value["size"] = [3]
    elif defect == "index":
        value["dimension"]["Region"]["category"]["index"]["02"] = 0
    else:
        del value["dimension"]["Region"]["category"]["label"]["02"]
    with pytest.raises(ValueError):
        validate_metadata(value, record())


def test_metadata_retries_source_change_with_fresh_catalogue():
    from harvesting.adapters.pxweb_v2.metadata import fetch_metadata

    changed = "2026-10-02T06:00:00Z"
    value = metadata()
    value["updated"] = changed

    def handler(request):
        return httpx.Response(
            200,
            json=value
            if request.url.path.endswith("/metadata")
            else {**table(), "language": "sv", "updated": changed},
        )

    async def run():
        client, _ = client_for(handler)
        async with client:
            return await fetch_metadata(record(), client=client)

    result = asyncio.run(run())
    assert result["updated"] == changed[:10]
    assert (
        result["extension"]["pxweb_v2"]["source_record"]["catalogue"]["updated"]
        == changed
    )


def test_codelist_validation_rejects_duplicate_values_and_wrong_language():
    from harvesting.adapters.pxweb_v2.validation import validate_codelist

    for value in [codelist("en"), {**codelist(), "values": codelist()["values"] * 2}]:
        with pytest.raises(ValueError):
            validate_codelist(deepcopy(value), "vs_region", "sv")


@pytest.mark.parametrize(
    "bad", [None, {"id": "bad"}, {"id": "good", "label": "duplicate"}]
)
def test_bad_catalogue_row_does_not_stop_later_pages(bad):
    from harvesting.adapters.pxweb_v2.discovery import discover_provider

    def handler(request):
        if request.url.path.endswith("/config"):
            return httpx.Response(200, json=CONFIG)
        number = int(request.url.params["pageNumber"])
        return httpx.Response(
            200,
            json={
                "language": request.url.params["lang"],
                "tables": [table("good"), bad] if number == 1 else [table("later")],
                "page": {
                    "pageNumber": number,
                    "pageSize": 2,
                    "totalElements": 3,
                    "totalPages": 2,
                },
            },
        )

    async def run():
        client, _ = client_for(handler)
        async with client:
            return [s async for s in discover_provider(PROVIDER, client=client)]

    for scope in asyncio.run(run()):
        assert not scope["complete"]
        assert scope["errors"]
        expected = (
            ["later"]
            if isinstance(bad, dict) and bad["id"] == "good"
            else ["good", "later"]
        )
        assert [r["dataset_code"] for r in scope["records"]] == expected

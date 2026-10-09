"""Metadata preservation, category coverage and real request-client boundaries."""

import asyncio
import json
import ssl
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from metadata_harvest import get_pxweb_metadata
from metadata_harvest.adapters.pxweb_v1.metadata import _generate, _merge, _read_sample


def table(url="https://example.test/Prefix/api/v1/sv/Database/Folder/T%20%C3%A5.px"):
    return dict(
        id="T å.px",
        title="Web title",
        published="2025-03",
        api_url=url,
        web_url="https://example.test/table?rxid=original",
        language="sv",
        database_id="Database",
        path="/Folder",
        category_path=[{"id": "Folder", "name": "Folder name"}],
        score=1.0,
    )


def metadata():
    return {
        "title": "GET title",
        "extension": {"custom": {"first": False}},
        "unknown": {"nested": [1, 2]},
        "variables": [
            {
                "code": "Area",
                "text": "GET area",
                "values": ["01", "02", "03", "04", "05"],
                "valueTexts": ["One", "Two", "Three", "Four", "Five"],
                "elimination": False,
                "mystery": {"value": "metadata value must survive"},
            },
            {
                "code": "Time",
                "text": "Year",
                "values": ["2023", "2024", "2025"],
                "valueTexts": ["2023", "2024", "2025"],
                "time": True,
            },
        ],
    }


class Provider:
    def __init__(self, source=None):
        self.source = source or metadata()
        self.full = metadata()
        self.calls = []
        self.count = 0
        self.interval = 0
        self.limit = 4
        self.mutate = lambda sample: sample

    async def json(self, url, payload=None, **kwargs):
        self.calls.append((url, deepcopy(payload)))
        self.count += 1
        if url.endswith("?config"):
            return {"maxCells": self.limit, "maxCalls": 10000, "timeWindow": 1}
        if payload is None:
            return deepcopy(self.source)
        selections = {item["code"]: item["selection"] for item in payload["query"]}
        sample = {
            "class": "dataset",
            "version": "2.0",
            "label": "POST title",
            "source": "Publisher",
            "updated": "2099-01-01",
            "value": [999999],
            "id": [],
            "size": [],
            "dimension": {},
            "status": "e",
            "note": ["Common"],
            "extension": {"custom": {"first": True, "second": 0}},
        }
        for variable in reversed(self.full["variables"]):
            code = variable["code"]
            selected = selections[code]
            values = (
                variable["values"]
                if selected["filter"] == "all"
                else variable["values"][:1]
                if selected["filter"] == "top"
                else selected["values"]
            )
            values = list(reversed(values))
            sample["id"].append(code)
            sample["size"].append(len(values))
            sample["dimension"][code] = {
                "label": "POST label",
                "category": {
                    "index": {value: i for i, value in enumerate(values)},
                    "label": {value: "POST " + value for value in values},
                    "unit": {
                        value: {"base": "Unit " + value, "decimals": 0}
                        for value in values
                    },
                    "note": {value: ["Note " + value] for value in values},
                },
            }
        sample["dimension"]["Hidden"] = {
            "label": "Eliminated",
            "category": {"unit": {"all": {"base": "People"}}},
        }
        return self.mutate(sample)


def test_full_coverage_preserves_metadata_and_precedence_without_observations():
    provider = Provider()
    original = table()
    result = asyncio.run(_generate(provider, original, "provider"))
    assert result["label"] == "POST title"
    assert "updated" not in result
    assert result["extension"]["pxweb_v1"]["updated_original"] == "2025-03"
    assert result["id"] == ["Area", "Time"]
    assert result["size"] == [5, 3]
    assert result["role"] == {"time": ["Time"]}
    assert result["value"] == [] and "status" not in result
    assert "999999" not in json.dumps(result)
    area = result["dimension"]["Area"]
    assert area["label"] == "GET area"
    assert area["category"]["label"]["05"] == "Five"
    assert area["category"]["unit"]["05"] == {"label": "Unit 05", "decimals": 0}
    assert area["category"]["note"]["05"] == ["Note 05"]
    assert (
        area["extension"]["pxweb_v1"]["metadata_harvest"]["pxweb"]["mystery"]["value"]
        == "metadata value must survive"
    )
    assert result["extension"]["pxweb_v1"]["custom"] == {"first": False, "second": 0}
    identity = {
        **result["extension"]["pxweb_v1"]["source_record"],
        **result["extension"]["pxweb_v1"]["harvest_metadata"],
    }
    assert identity["pxweb"]["unknown"] == {"nested": [1, 2]}
    assert identity["provider_code"] == "provider"
    assert identity["dataset_code"] == original["id"]
    assert identity["web_url"] == original["web_url"]
    assert identity["sample_status"]
    assert (
        identity["auxiliary_dimensions"]["Hidden"]["category"]["unit"]["all"]["base"]
        == "People"
    )
    assert all(url == original["api_url"] for url, _ in provider.calls[1:])
    for _, payload in provider.calls:
        if payload:
            assert len(payload["query"]) == 2
    assert original == table()


@pytest.mark.parametrize("missing", ["values", "valueTexts", "both"])
def test_overflow_recovery_and_known_list_chunking(missing):
    source = metadata()
    for key in ["values", "valueTexts"] if missing == "both" else [missing]:
        source["variables"][0][key] = None
    provider = Provider(source)
    if missing != "valueTexts":
        provider.limit = 10
    result = asyncio.run(_generate(provider, table(), "p"))
    assert result["size"] == [5, 3]
    assert len(result["dimension"]["Area"]["category"]["unit"]) == 5


def test_multiple_unknown_dimensions_are_explicitly_restricted():
    source = metadata()
    for variable in source["variables"]:
        variable["values"] = variable["valueTexts"] = None
    provider = Provider(source)
    provider.limit = 10
    result = asyncio.run(_generate(provider, table(), "p"))
    assert sorted(result["size"]) == [3, 5]
    first = provider.calls[2][1]["query"]
    assert [item["selection"]["filter"] for item in first] == ["all", "top"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: {**s, "id": ["Other", "Area"]},
        lambda s: {**s, "size": [100000, 2]},
        lambda s: {**s, "version": "1.0"},
    ],
)
def test_structural_changes_fail(mutation):
    provider = Provider()
    provider.mutate = mutation
    with pytest.raises(ValueError):
        asyncio.run(_generate(provider, table(), "p"))


def test_unknown_oversized_dimension_fails_instead_of_partial_success():
    source = metadata()
    source["variables"][0]["values"] = None
    with pytest.raises(ValueError, match="oversized"):
        asyncio.run(_generate(Provider(source), table(), "p"))


@pytest.mark.parametrize("value", [False, 0, "", [], {}])
def test_valid_empty_values_are_not_overwritten(value):
    assert _merge(value, "replacement") == value


def test_notes_and_links_union_but_other_arrays_keep_precedence():
    result = _merge(
        {"note": ["a"], "link": {"item": [{"href": "a"}]}, "x": [1]},
        {"note": ["a", "b"], "link": {"item": [{"href": "b"}]}, "x": [2]},
    )
    assert result == {
        "note": ["a", "b"],
        "link": {"item": [{"href": "a"}, {"href": "b"}]},
        "x": [1],
    }


def test_invalid_input_fails_before_playwright(monkeypatch):
    def forbidden():
        pytest.fail("Playwright must not start")

    monkeypatch.setattr(
        "metadata_harvest.adapters.pxweb_v1.metadata.async_playwright", forbidden
    )
    with pytest.raises(ValueError):
        asyncio.run(get_pxweb_metadata({}, provider_code="p"))


@pytest.fixture
def server():
    provider = Provider()
    state = {"statuses": [], "paths": [], "retry": "0", "provider": provider}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(None)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.respond(payload)

        def respond(self, payload):
            state["paths"].append(self.path)
            status = state["statuses"].pop(0) if state["statuses"] else 200
            result = asyncio.run(provider.json(self.path, payload))
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", state["retry"])
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield http, state
    http.shutdown()
    http.server_close()
    thread.join()


def test_public_operation_real_playwright_requests_retries_and_encoding(server):
    http, state = server
    state["statuses"] = [429, 503]
    state["retry"] = "Tue, 06 Oct 2020 00:00:00 GMT"
    url = f"http://127.0.0.1:{http.server_port}/Prefix/api/v1/sv/Db/F/T%20%C3%A5.px"
    result = asyncio.run(get_pxweb_metadata(table(url), provider_code="p"))
    assert result["size"] == [5, 3]
    assert state["paths"][:3] == ["/Prefix/api/v1/sv/?config"] * 3
    assert all(path.endswith("T%20%C3%A5.px") for path in state["paths"][3:])


def test_public_certificate_recovery(server, caplog):
    http, _ = server
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    fixtures = Path(__file__).parent / "fixtures"
    context.load_cert_chain(
        fixtures / "localhost-cert.pem", fixtures / "localhost-key.pem"
    )
    http.socket = context.wrap_socket(http.socket, server_side=True)
    url = f"https://127.0.0.1:{http.server_port}/api/v1/sv/Db/T.px"
    result = asyncio.run(get_pxweb_metadata(table(url), provider_code="p"))
    assert result["value"] == []
    assert "ignore_https_errors=True" in caplog.text


def test_cancellation_disposes_request_context(monkeypatch):
    from metadata_harvest.adapters.pxweb_v1 import metadata as module

    closed = []

    async def generate(requests, *args):
        requests.context = await requests.playwright.request.new_context()
        raise asyncio.CancelledError

    original = module._Requests.close

    async def close(self):
        await original(self)
        closed.append(True)

    monkeypatch.setattr(module, "_generate", generate)
    monkeypatch.setattr(module._Requests, "close", close)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(get_pxweb_metadata(table(), provider_code="p"))
    assert closed == [True]


def test_sample_cannot_hide_unrequested_categories():
    provider = Provider()
    sample = asyncio.run(
        provider.json(
            "url",
            {
                "query": [
                    {
                        "code": "Area",
                        "selection": {"filter": "item", "values": ["01", "02"]},
                    },
                    {
                        "code": "Time",
                        "selection": {"filter": "item", "values": ["2023"]},
                    },
                ]
            },
        )
    )
    with pytest.raises(ValueError, match="selection"):
        _read_sample(
            sample, metadata()["variables"], {"Area": ["01"], "Time": ["2023"]}, 10
        )


def test_eliminated_singleton_code_is_reconciled_without_losing_units():
    provider = Provider()
    provider.full["variables"][0].update(values=["A10"], valueTexts=["Contents"])
    provider.source = deepcopy(provider.full)

    def eliminate(sample):
        category = sample["dimension"]["Area"]["category"]
        for field in category.values():
            if isinstance(field, dict) and "A10" in field:
                field["EliminatedValue"] = field.pop("A10")
        sample["dimension"]["Area"]["extension"] = {"elimination": True}
        return sample

    provider.mutate = eliminate
    result = asyncio.run(_generate(provider, table(), "p"))
    assert result["dimension"]["Area"]["category"]["unit"]["A10"]["label"] == "Unit A10"
    assert result["dimension"]["Area"]["category"]["label"] == {"A10": "Contents"}


def test_synthetic_singleton_dimension_and_its_role_are_preserved():
    provider = Provider()

    def synthetic(sample):
        sample["id"].append("ContentsCode")
        sample["size"].append(1)
        sample["dimension"]["ContentsCode"] = {
            "category": {
                "index": ["EliminatedValue"],
                "label": {"EliminatedValue": "Count"},
            },
            "extension": {"elimination": True},
        }
        sample["role"] = {"metric": ["ContentsCode"]}
        return sample

    provider.mutate = synthetic
    result = asyncio.run(_generate(provider, table(), "p"))
    assert result["id"] == ["Area", "Time"]
    assert result["extension"]["pxweb_v1"]["harvest_metadata"]["auxiliary_roles"] == {
        "metric": ["ContentsCode"]
    }
    assert (
        "ContentsCode"
        in result["extension"]["pxweb_v1"]["harvest_metadata"]["auxiliary_dimensions"]
    )


def test_note_mandatory_positions_follow_merged_notes():
    result = _merge(
        {"note": ["Old"], "extension": {"noteMandatory": {"0": False}}},
        {
            "note": ["New", "Old"],
            "extension": {"noteMandatory": {"0": True, "1": True}},
        },
    )
    assert result["note"] == ["Old", "New"]
    assert result["extension"]["noteMandatory"] == {"0": False, "1": True}


def test_table_standard_metadata_has_highest_precedence():
    record = table()
    record.update(source="Table source", note=["Table note"])
    result = asyncio.run(_generate(Provider(), record, "p"))
    assert result["source"] == "Table source"
    assert result["note"] == ["Table note", "Common"]


def test_http_failure_is_not_a_successful_empty_dataset(server):
    http, state = server
    state["statuses"] = [200, 404]
    with pytest.raises(RuntimeError, match="GET table metadata.*HTTP 404"):
        asyncio.run(
            get_pxweb_metadata(
                table(f"http://127.0.0.1:{http.server_port}/api/v1/sv/Db/t.px"),
                provider_code="p",
            )
        )

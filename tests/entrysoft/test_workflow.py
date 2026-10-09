"""Incremental RowStore lifecycle against a fake EntryScape instance."""

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from metadata_harvest.adapters.entrysoft.client import Client
from metadata_harvest.adapters.entrysoft.state import (
    collection_store,
    load_current,
    load_tables,
)
from tests.entrysoft.conftest import page

NOW = datetime(2026, 11, 1, 6, tzinfo=UTC)
DCAT = "http://www.w3.org/ns/dcat#"
DCTERMS = "http://purl.org/dc/terms/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
PROVIDERS = [
    {
        "code": "alpha",
        "label": "Alpha kommun",
        "base_url": "https://alpha.example",
        "languages": ["sv"],
    },
    {
        "code": "beta",
        "label": "Beta kommun",
        "base_url": "https://beta.example",
        "languages": ["sv"],
    },
]


def rows(n, prefix="A"):
    return [
        {
            "ar": str(2020 + i % 3),
            "omrade": f"{prefix}{i % 4}",
            "antal": str(i),
            "kommun_id": "1",
        }
        for i in range(n)
    ]


class FakeEntryScape:
    """EntryStore search plus RowStore tables for several hosts in one transport."""

    def __init__(self):
        self.tables: dict[
            str, dict
        ] = {}  # host -> uuid -> {"rows", "title", "modified"}
        self.down: set[str] = set()  # hosts whose search fails
        self.throttle: set[str] = set()  # hosts answering 429
        self.calls: list[str] = []

    def add(self, host, uuid, title, rows, modified="2026-01-01T00:00:00Z"):
        self.tables.setdefault(host, {})[uuid] = {
            "rows": rows,
            "title": title,
            "modified": modified,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        host = url.hostname
        self.calls.append(f"{host}{url.path}")
        if host in self.throttle:
            return httpx.Response(429, headers={"Retry-After": "1"})
        if url.path.endswith("/store/search"):
            if host in self.down:
                return httpx.Response(500)
            query = dict(parse_qsl(url.query)).get("query", "")
            if query.startswith("resource:("):
                return httpx.Response(
                    200, json={"resource": {"children": self._datasets(host)}}
                )
            if "rowstore" not in query:
                return httpx.Response(200, json={"resource": {"children": []}})
            return httpx.Response(
                200, json={"resource": {"children": self._distributions(host)}}
            )
        if "/rowstore/dataset/" in url.path:
            uuid = url.path.rsplit("/", 1)[1]
            table = self.tables.get(host, {}).get(uuid)
            if table is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=page(
                    table["rows"], dict(parse_qsl(url.query, keep_blank_values=True))
                ),
            )
        return httpx.Response(404)

    def _distributions(self, host):
        children = []
        for uuid, table in self.tables.get(host, {}).items():
            dist = f"https://{host}/store/1/resource/d-{uuid}"
            dataset = f"https://{host}/store/1/resource/s-{uuid}"
            metadata = {
                RDF_TYPE: [{"value": DCAT + "Distribution"}],
                DCAT + "accessURL": [
                    {"value": f"https://{host}/rowstore/dataset/{uuid}"}
                ],
                DCTERMS + "title": [{"value": table["title"] + " - API", "lang": "sv"}],
            }
            if table["modified"]:
                metadata[DCTERMS + "modified"] = [{"value": table["modified"]}]
            children.append(
                {
                    "contextId": "1",
                    "entryId": "d-" + uuid,
                    "metadata": {dist: metadata},
                    "relations": {dataset: {DCAT + "distribution": [{"value": dist}]}},
                }
            )
        return children

    def _datasets(self, host):
        return [
            {
                "contextId": "1",
                "entryId": "s-" + uuid,
                "metadata": {
                    f"https://{host}/store/1/resource/s-{uuid}": {
                        RDF_TYPE: [{"value": DCAT + "Dataset"}],
                        DCTERMS + "title": [{"value": table["title"], "lang": "sv"}],
                    }
                },
            }
            for uuid, table in self.tables.get(host, {}).items()
        ]


UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def source():
    fake = FakeEntryScape()
    fake.add("alpha.example", UUID_A, "Befolkning 2023", rows(40))
    fake.add("alpha.example", UUID_B, "Befolkning 2024", rows(40, "B"), modified=None)
    fake.add("beta.example", UUID_C, "Lekplatser", rows(10))
    return fake


def execute(fake, data, output, **options):
    from metadata_harvest.adapters.entrysoft.workflow import collect, run

    transport = httpx.MockTransport(fake)

    def factory(base_url, pacer):
        return Client(base_url, pacer=pacer, transport=transport)

    result = asyncio.run(
        run(
            PROVIDERS,
            data,
            output,
            client_factory=factory,
            clock=NOW,
            preflight=False,
            requests_per_second=1e6,
            **options,
        )
    )
    collect(data, output)
    return result


def table_calls(fake):
    return [c for c in fake.calls if "/rowstore/" in c]


@pytest.fixture
def data(source, tmp_path):
    root = tmp_path / "data"
    result = execute(source, root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["harvested"] == 3 and result["counts"]["datasets"] == 2
    source.calls.clear()
    return root


def test_bootstrap_publishes_merged_and_single_datasets(data):
    from metadata_harvest.validation import validate_metadata

    documents = list(collection_store(data).records.values())
    for document in documents:
        validate_metadata(document, adapter="entrysoft")
    codes = {d["extension"]["nordicintel"]["dataset_code"] for d in documents}
    assert codes == {"alpha:befolkning", UUID_C}
    merged = next(d for d in documents if d["id"][0] == "part")
    assert list(merged["dimension"]["part"]["category"]["index"]) == ["2023", "2024"]
    assert set(load_tables(data)) == {
        f"alpha/{UUID_A}",
        f"alpha/{UUID_B}",
        f"beta/{UUID_C}",
    }
    assert load_current(data)["providers"]["alpha"]["complete"] is True


def test_unchanged_tables_cost_nothing_and_markerless_tables_one_request(
    source, data, tmp_path
):
    before = {p.name: p.read_bytes() for p in (data / "metadata").glob("*.jsonl.gz")}
    result = execute(source, data, tmp_path / "rerun")
    assert result["counts"]["unchanged"] == 3 and result["counts"]["harvested"] == 0
    assert table_calls(source) == [
        f"alpha.example/rowstore/dataset/{UUID_B}"
    ]  # no markers: row count
    assert {
        p.name: p.read_bytes() for p in (data / "metadata").glob("*.jsonl.gz")
    } == before


def test_changed_marker_and_changed_rowcount_reharvest(source, data, tmp_path):
    source.tables["alpha.example"][UUID_A]["modified"] = "2026-06-01T00:00:00Z"
    source.tables["alpha.example"][UUID_B]["rows"] = rows(45, "B")
    result = execute(source, data, tmp_path / "changed")
    assert result["counts"]["harvested"] == 2 and result["counts"]["unchanged"] == 1
    tables = load_tables(data)
    assert tables[f"alpha/{UUID_B}"]["rowcount"] == 45
    assert (
        tables[f"alpha/{UUID_A}"]["markers"]["distribution_modified"]
        == "2026-06-01T00:00:00Z"
    )


def test_discovery_failure_keeps_tables_and_complete_discovery_removes(
    source, data, tmp_path
):
    source.down.add("beta.example")
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert f"beta/{UUID_C}" in load_tables(data) and result["counts"]["removed"] == 0

    source.down.clear()
    del source.tables["beta.example"][UUID_C]
    result = execute(source, data, tmp_path / "removed")
    assert result["counts"]["removed"] == 1
    assert f"beta/{UUID_C}" not in load_tables(data)
    assert {
        d["extension"]["nordicintel"]["dataset_code"]
        for d in collection_store(data).records.values()
    } == {"alpha:befolkning"}


def test_budget_stop_keeps_finished_work(source, tmp_path):
    data = tmp_path / "data"
    result = execute(source, data, tmp_path / "budget", max_requests=4)
    assert result["interrupted"] and result["status"] == "completed_with_errors"
    assert 0 < result["counts"]["harvested"] < 3
    assert len(load_tables(data)) == result["counts"]["harvested"]
    source.calls.clear()
    result = execute(source, data, tmp_path / "continue")
    assert result["status"] == "success" and len(load_tables(data)) == 3


def test_second_throttle_stops_the_run(source, data, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "metadata_harvest.adapters.entrysoft.client.MIN_THROTTLE_PAUSE", 0.0
    )
    source.throttle.add("alpha.example")
    result = execute(source, data, tmp_path / "throttled")
    assert result["interrupted"] and "429" in json.dumps(result["errors"])


def test_bounded_run_touches_only_selected_providers(source, data, tmp_path):
    del source.tables["beta.example"][UUID_C]
    result = execute(source, data, tmp_path / "bounded", selected=["alpha"])
    assert result["counts"]["removed"] == 0 and f"beta/{UUID_C}" in load_tables(data)
    assert all(call.startswith("alpha.example") for call in source.calls)


def test_stale_artifact_rejected(source, data, tmp_path):
    from metadata_harvest.adapters.entrysoft.workflow import collect, run

    transport = httpx.MockTransport(source)
    asyncio.run(
        run(
            PROVIDERS,
            data,
            tmp_path / "out",
            client_factory=lambda b, p: Client(b, pacer=p, transport=transport),
            clock=NOW,
            preflight=False,
            requests_per_second=1e6,
        )
    )
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out")

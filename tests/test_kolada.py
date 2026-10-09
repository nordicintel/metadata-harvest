"""Kolada tests use tiny fixtures; trusted seeds never get corpus validation."""

import asyncio
import json

import pytest

from metadata_harvest.adapters.kolada.metadata import build_dataset
from metadata_harvest.adapters.kolada.state import load_documents, load_state
from metadata_harvest.validation import validate_metadata
from tests.kolada_fixture import initial_state as import_seed


def test_change_probe_path_pagination_and_transient_splitting():
    import httpx

    from metadata_harvest.adapters.kolada.client import KoladaClient

    calls = []

    def handler(request):
        calls.append(str(request.url))
        path = request.url.path
        if "N1,N2" in path:
            return httpx.Response(504)
        rows = (
            [{"kpi": "N1", "period": 2025, "ou": "O1", "values": []}]
            if "/N1/" in path
            else []
        )
        return httpx.Response(200, json={"values": rows, "next_url": None})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = KoladaClient({"rate_limit": 0}, http=http)
            assert await client.changed("ou", ["N1", "N2"], [2025], "2026-01-01") == {
                "N1"
            }

    asyncio.run(check())
    assert any("oudata/kpi/N1/year/2025" in c for c in calls)
    assert all("from_date=2026-01-01" in c for c in calls)


def test_coverage_keeps_suppression_but_not_deleted_rows():
    import httpx

    from metadata_harvest.adapters.kolada.client import KoladaClient

    rows = [
        {
            "kpi": "N1",
            "period": 2025,
            "ou": "O1",
            "values": [
                {"gender": "K", "value": None, "status": "Privacy", "isdeleted": False}
            ],
        },
        {
            "kpi": "N1",
            "period": 2024,
            "ou": "O2",
            "values": [{"gender": "T", "value": 1, "isdeleted": True}],
        },
    ]

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"values": rows, "next_url": None})
            )
        ) as http:
            client = KoladaClient({"rate_limit": 0}, http=http)
            return await client.coverage("ou", "N1", [2024, 2025], {})

    assert asyncio.run(check()) == {"years": [2025], "genders": ["K"], "ou_ids": ["O1"]}


@pytest.fixture
def seed_source(tmp_path):
    source = tmp_path / "snapshot"
    source.mkdir()
    files = {
        "kpi.json": [
            {
                "id": "N1",
                "title": "Test",
                "description": "Källa: SCB.",
                "has_ou_data": True,
                "modified": "2026-01-02",
                "modified_source": "https://api.kolada.se/v3/data/",
                "modified_checked_at": "2026-10-09T04:00:00Z",
                "modified_evidence": json.dumps({"periods": list(range(1970, 2032))}),
                "data_eu_modified": "2099-01-01",
                "title_en": "Portal title",
            }
        ],
        "municipality.json": [{"id": "0180", "title": "Stockholm"}],
        "ou.json": [{"id": "O1", "title": "Unit", "municipality": "0180"}],
        "kpi_groups.json": [],
        "municipality_groups.json": [],
        "report.json": {
            "snapshot_started_at": "2026-10-09T05:00:00Z",
            "scrape_state": [{"last_full_scrape_completed_at": "2026-10-08T16:00:00Z"}],
        },
    }
    for name, value in files.items():
        (source / name).write_text(json.dumps(value), encoding="utf-8")
    coverage = {
        "kpi": "N1",
        "years": [2025],
        "genders": ["T"],
        "municipality_ids": ["0180"],
        "municipality_group_ids": [],
    }
    (source / "municipality_coverage.jsonl").write_text(json.dumps(coverage) + "\n")
    (source / "ou_coverage.jsonl").write_text(
        json.dumps(
            {"kpi": "N1", "years": [2025], "genders": ["M", "K", "T"], "ou_ids": ["O1"]}
        )
        + "\n"
    )
    return source


def test_seed_is_trusted_no_validation_or_network(seed_source, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "metadata_harvest.validation.validate_metadata",
        lambda *a, **k: pytest.fail("Seed validation forbidden"),
    )
    target = tmp_path / "data"
    result = import_seed(seed_source, target)
    assert result["seeded"] == 2
    docs = load_documents(target)
    assert set(docs) == {"municipality/N1", "ou/N1"}
    assert docs["municipality/N1"]["updated"] == "2026-01-02"
    assert docs["ou/N1"]["value"] == []
    assert "geo" not in docs["ou/N1"]["role"]
    assert "data_eu" not in json.dumps(docs)
    assert "Portal title" not in json.dumps(docs)
    saved = load_state(target)["scopes"]["municipality/N1"]
    assert saved["checked_at"] == "2026-10-09T04:00:00Z"
    assert saved["coverage_at"] == "2026-10-08T16:00:00Z"
    before = (target / "metadata/kolada__sv.jsonl.gz").read_bytes()
    import_seed(seed_source, target)
    assert (target / "metadata/kolada__sv.jsonl.gz").read_bytes() == before


def test_sample_mapping_preserves_suppressed_gender_and_group(seed_source, tmp_path):
    target = tmp_path / "data"
    import_seed(seed_source, target)
    for document in load_documents(target).values():
        validate_metadata(document, adapter="kolada")
    state = load_state(target)
    cov = {
        "years": [2025],
        "genders": ["T"],
        "municipality_ids": ["0180"],
        "municipality_group_ids": ["G1"],
    }
    refs = state["references"]
    refs["municipality_groups"]["G1"] = {"id": "G1", "title": "Group", "members": []}
    doc = build_dataset(state["kpis"]["N1"], "municipality", cov, refs)
    assert doc["dimension"]["municipality"]["category"]["index"] == {"0180": 0, "G1": 1}
    validate_metadata(doc, adapter="kolada")


def test_request_budget_includes_waits_and_does_not_retry():
    from time import monotonic

    import httpx

    from metadata_harvest.adapters.kolada.client import BudgetExpired, KoladaClient

    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200, json={"values": []})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = KoladaClient({"rate_limit": 0}, http=http)
            client.deadline = monotonic() + 0.01
            with pytest.raises(BudgetExpired):
                await client.listing("kpi")

    asyncio.run(check())
    assert len(calls) == 1

"""Kolada tests use tiny fixtures; trusted seeds never get corpus validation."""

import asyncio
import json
from datetime import date

import httpx
import pytest

from apps.harvest.metadata_adapters.kolada import coverage as cov
from apps.harvest.metadata_adapters.kolada.client import BudgetExpired, KoladaClient
from apps.harvest.metadata_adapters.kolada.metadata import (
    build_dataset,
    updated_for,
    years_for,
)
from apps.harvest.metadata_adapters.kolada.state import load_state, normalize
from apps.harvest.validation import validate_metadata
from tests.support.kolada_fixture import initial_state as import_seed
from tests.support.parsing import documents


def load_documents(root):
    """Parsed documents keyed ``<scope>/<kpi>``."""
    parsed = documents("kolada", root, [{"code": "kolada"}])
    return {key.split("/", 1)[1]: document for key, document in parsed.items()}


def cell(kpi, period, entity, gender="T", deleted=False):
    return {
        "kpi": kpi,
        "period": period,
        "entity": entity,
        "gender": gender,
        "deleted": deleted,
    }


def run_client(handler, coroutine):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await coroutine(KoladaClient({"rate_limit": 0}, http=http))

    return asyncio.run(check())


def test_change_scan_batches_windows_and_flattens_cells():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        rows = (
            [
                {
                    "kpi": "N1",
                    "period": 2025,
                    "municipality": "0180",
                    "values": [
                        {"gender": "T", "value": 1.0, "isdeleted": False},
                        {"gender": "K", "value": None, "isdeleted": True},
                    ],
                }
            ]
            if "/data/municipality/0000," in request.url.path
            else []
        )
        return httpx.Response(200, json={"values": rows, "next_url": None})

    ids = [f"{i:04d}" for i in range(30)]
    records = run_client(
        handler,
        lambda c: c.changed_records("municipality", ids, [2024, 2025], "2026-10-01"),
    )
    assert records == [
        cell("N1", 2025, "0180"),
        cell("N1", 2025, "0180", "K", deleted=True),
    ]
    assert len(calls) == 2  # 30 ids -> two batches, two years -> one window
    assert all("from_date=2026-10-01" in c and "per_page=5000" in c for c in calls)
    assert any("/data/municipality/0000," in c for c in calls)


def test_change_scan_fails_as_a_whole_when_one_window_fails():
    def handler(request):
        if "N2" in request.url.path:
            return httpx.Response(504)
        return httpx.Response(200, json={"values": [], "next_url": None})

    with pytest.raises(ValueError, match="change windows failed"):
        run_client(
            handler,
            lambda c: c.changed_records(
                "ou", ["N1"] + [f"N2{i}" for i in range(25)], [2025], "2026-10-01"
            ),
        )


def test_single_kpi_rescan_follows_pagination_and_checks_scope():
    pages = {
        1: {
            "values": [
                {"kpi": "N1", "period": 2024, "ou": "O1", "values": [{"gender": "T"}]}
            ],
            "next_url": "x",
        },
        2: {
            "values": [
                {"kpi": "N1", "period": 2025, "ou": "O2", "values": [{"gender": "M"}]}
            ],
            "next_url": None,
        },
    }

    def handler(request):
        assert request.url.path.endswith("/oudata/kpi/N1/year/2024,2025")
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    records = run_client(handler, lambda c: c.scope_records("ou", "N1", [2024, 2025]))
    assert records == [cell("N1", 2024, "O1"), cell("N1", 2025, "O2", "M")]

    def wrong(request):
        return httpx.Response(
            200,
            json={
                "values": [
                    {
                        "kpi": "N9",
                        "period": 2024,
                        "ou": "O1",
                        "values": [{"gender": "T"}],
                    }
                ],
                "next_url": None,
            },
        )

    with pytest.raises(ValueError, match="outside requested"):
        run_client(wrong, lambda c: c.scope_records("ou", "N1", [2024]))


def test_coverage_merge_keeps_suppressed_cells_skips_deleted_and_filters():
    inventory = {"0180", "G1"}
    records = [
        cell("N1", 2025, "0180", "K"),
        cell("N1", 2024, "0180", "T", deleted=True),
        cell("N1", 2026, "G1"),
        cell("N1", 2026, "G999"),  # derivable comparison group, not inventoried
    ]
    assert cov.rebuild("municipality", records, entities=inventory) == {
        "years": [2025, 2026],
        "genders": ["K", "T"],
        "municipality_ids": ["0180"],
        "municipality_group_ids": ["G1"],
    }
    merged = cov.merge(
        "ou",
        {"years": [2023], "genders": ["T"], "ou_ids": ["O1"]},
        [cell("N1", 2025, "O2", "M")],
    )
    assert merged == {
        "years": [2023, 2025],
        "genders": ["M", "T"],
        "ou_ids": ["O1", "O2"],
    }


def test_years_follow_scope_rules():
    assert years_for("municipality", date(2026, 10, 10))[-1] == 2028
    assert years_for("municipality", date(2026, 10, 10))[0] == 1970
    assert years_for("ou", date(2026, 10, 10)) == list(range(2003, 2027))


def test_updated_date_rules():
    today = date(2026, 10, 10)
    assert updated_for({"publication_date": "2027-08-28"}, 2025, today) == "2026-08-28"
    assert updated_for({"publication_date": "2026-03-01"}, 2025, today) == "2026-03-01"
    assert updated_for({"publication_date": "2028-02-29"}, 2025, today) == "2027-02-28"
    assert updated_for({"publication_date": None}, 2025, today, "2026-05-05") == (
        "2026-05-05"
    )
    assert updated_for({}, 2024, today) == "2025-01-01"
    assert updated_for({}, 2026, today) == "2026-10-10"
    assert updated_for({}, None, today) == "2026-10-10"


def test_normalize_reduces_legacy_state():
    legacy = {
        "kpis": {},
        "references": {},
        "pending": {"ou/N1": "data", "municipality/N1": "metadata"},
        "scopes": {
            "ou/N1": {
                "coverage": {"years": [2025]},
                "periods": [2025],
                "checked_at": "2026-10-10T10:50:09+00:00",
                "coverage_at": "2026-10-03T04:00:00+00:00",
                "modified": "2026-01-01",
            }
        },
    }
    state = normalize(legacy)
    assert state["refreshed"] == "2026-10-03"
    assert state["scopes"] == {"ou/N1": {"coverage": {"years": [2025]}, "empty": False}}
    assert state["pending"] == {"ou/N1": "rescan", "municipality/N1": "rebuild"}
    assert normalize({})["refreshed"] is None


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
                "title_en": "Portal title",
            }
        ],
        "ou.json": [{"id": "O1", "title": "Unit", "municipality": "0180"}],
        "kpi_groups.json": [],
        "municipality_groups.json": [],
    }
    for name, value in files.items():
        (source / name).write_text(json.dumps(value), encoding="utf-8")
    coverage = {
        "years": [2025],
        "genders": ["T"],
        "municipality_ids": ["0180"],
        "municipality_group_ids": [],
    }
    (source / "municipality_coverage.jsonl").write_text(json.dumps(coverage) + "\n")
    (source / "ou_coverage.jsonl").write_text(
        json.dumps({"years": [2025], "genders": ["M", "K", "T"], "ou_ids": ["O1"]})
        + "\n"
    )
    return source


def test_seed_is_trusted_no_validation_or_network(seed_source, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "apps.harvest.validation.validate_metadata",
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
    assert "Portal title" not in json.dumps(docs)
    assert load_state(target)["refreshed"] == "2026-10-08"
    before = (target / "source/manifest.json").read_bytes()
    import_seed(seed_source, target)
    assert (target / "source/manifest.json").read_bytes() == before


def test_sample_mapping_preserves_suppressed_gender_and_group(seed_source, tmp_path):
    target = tmp_path / "data"
    import_seed(seed_source, target)
    for document in load_documents(target).values():
        validate_metadata(document, adapter="kolada")
    state = load_state(target)
    coverage = {
        "years": [2025],
        "genders": ["T"],
        "municipality_ids": ["0180"],
        "municipality_group_ids": ["G2328"],
    }
    refs = state["references"]
    refs["municipality_groups"]["G2328"] = {"id": "G2328", "title": "G", "members": []}
    doc = build_dataset(
        state["kpis"]["N1"], "municipality", coverage, refs, updated="2026-01-02"
    )
    assert doc["dimension"]["municipality"]["category"]["index"] == {
        "0180": 0,
        "G2328": 1,
    }
    assert doc["dimension"]["municipality"]["category"]["label"]["0180"] == "Stockholm"
    assert "gender" not in doc["dimension"]
    validate_metadata(doc, adapter="kolada")


def test_request_budget_includes_waits_and_does_not_retry():
    from time import monotonic

    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200, json={"values": []})

    async def check(client):
        client.deadline = monotonic() + 0.01
        with pytest.raises(BudgetExpired):
            await client.listing("kpi")

    run_client(handler, check)
    assert len(calls) == 1

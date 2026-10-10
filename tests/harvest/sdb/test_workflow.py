"""Weekly SDB lifecycle against the in-process SDB imitation."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.harvest.metadata_adapters.sdb.client import SdbClient, Unreachable
from apps.harvest.metadata_adapters.sdb.state import (
    collection,
    load_documents,
    load_state,
)

SDB_DATA = json.loads(
    (Path("tests/fixtures/sdb") / "sdb.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 10, 12, 5, tzinfo=UTC)
PROVIDER = {
    "code": "socialstyrelsen",
    "languages": ["sv", "en"],
    "retry_cooldown_seconds": 0,
}
LANGUAGES = PROVIDER["languages"]


def first_codes_row(subject: str, **overrides) -> dict:
    """A result row using each variable's first category code."""
    row = {}
    for variable in SDB_DATA["variables"]["sv"][subject]:
        name = variable["namn"]
        key = "ar" if name == "ar" else f"{name}Id"
        row[key] = SDB_DATA["categories"]["sv"][subject][name][0]["id"]
    return {**row, **overrides, "varde": "1"}


@pytest.fixture
def seeded(sdb):
    for subject in SDB_DATA["variables"]["sv"]:
        sdb.fake.rows[subject] = [first_codes_row(subject)]
    return sdb


def start(sdb, data, output, *, clock=NOW, **options):
    from apps.harvest.metadata_adapters.sdb.workflow import run

    async def main():
        client = SdbClient({"base_url": sdb.api_root}, retry_delay=0)
        try:
            return await run(
                PROVIDER, data, output, client=client, clock=clock, **options
            )
        finally:
            await client.close()

    return asyncio.run(main())


def execute(sdb, data, output, **options):
    from apps.harvest.metadata_adapters.sdb.workflow import collect

    result = start(sdb, data, output, **options)
    collect(data, output, languages=LANGUAGES)
    return result


def published(data):
    return set(load_documents(data, LANGUAGES))


@pytest.fixture
def data(seeded, tmp_path):
    root = tmp_path / "data"
    result = execute(seeded, root, tmp_path / "bootstrap")
    assert result["status"] == "success"
    assert result["counts"]["rebuilt"] == 4
    return root


def test_bootstrap_publishes_both_languages(data):
    from apps.harvest.validation import validate_metadata

    assert published(data) == {
        "sv/amning",
        "sv/dodsorsaker",
        "sv/drgstatistikislutenvard",
        "en/dodsorsaker",
    }
    for document in load_documents(data, LANGUAGES).values():
        validate_metadata(document, adapter="sdb")
        assert document["extension"]["socialstyrelsen"]["source_record"]["namn"]
    english = load_documents(data, ["en"])["en/dodsorsaker"]
    assert english["extension"]["nordicintel"]["time_unit"] == "annual"
    assert (data / collection("en")).exists()


def test_rerun_is_byte_identical_and_unchanged(seeded, data, tmp_path):
    before = {lang: (data / collection(lang)).read_bytes() for lang in LANGUAGES}
    result = execute(seeded, data, tmp_path / "rerun")
    assert result["counts"]["unchanged"] == 4
    assert result["counts"]["rebuilt"] == 0
    assert {lang: (data / collection(lang)).read_bytes() for lang in LANGUAGES} == (
        before
    )


def test_english_listing_failure_withholds_only_english(seeded, data, tmp_path):
    seeded.fake.failing_paths.add("/api/v1/en")
    result = execute(seeded, data, tmp_path / "en-down")
    assert result["status"] == "completed_with_errors"
    assert published(data) == {
        "sv/amning",
        "sv/dodsorsaker",
        "sv/drgstatistikislutenvard",
    }
    assert set(load_state(data)["withheld"]) == {"en/dodsorsaker"}

    seeded.fake.failing_paths.clear()
    result = execute(seeded, data, tmp_path / "en-back")
    assert result["counts"]["restored"] == 1
    assert "en/dodsorsaker" in published(data)


def test_failing_result_endpoint_is_withheld_then_restored(seeded, data, tmp_path):
    before = load_documents(data, LANGUAGES)["sv/amning"]
    seeded.fake.failing_paths.add("/api/v1/sv/amning/resultat")
    seeded.fake.requests.clear()
    result = execute(seeded, data, tmp_path / "down")
    assert result["counts"]["withheld"] == 1
    assert "sv/amning" not in published(data)
    state = load_state(data)
    assert state["withheld"]["sv/amning"] == before
    assert state["subjects"]["sv/amning"]["error"]["stage"] == "endpoint"
    probes = seeded.fake.requests.count("/api/v1/sv/amning/resultat?per_sida=1")
    assert probes == 6  # three attempts, then one end-of-run retry pass

    seeded.fake.failing_paths.clear()
    result = execute(seeded, data, tmp_path / "back")
    assert result["counts"]["restored"] == 1
    assert load_documents(data, LANGUAGES)["sv/amning"] == before


def test_mapping_failure_keeps_document_until_stale(seeded, data, tmp_path):
    before = load_documents(data, LANGUAGES)["sv/amning"]
    seeded.fake.result_names["amning"] = {"variabel": "grupp", "alder": "age"}
    row = first_codes_row("amning")
    row["gruppId"] = row.pop("variabelId")
    row["ageId"] = row.pop("alderId")
    seeded.fake.rows["amning"] = [row]

    result = execute(seeded, data, tmp_path / "broken")
    assert result["errors"][0]["stage"] == "metadata"
    assert "Cannot match SDB variables" in result["errors"][0]["error"]
    assert load_documents(data, LANGUAGES)["sv/amning"] == before

    execute(seeded, data, tmp_path / "stale", clock=NOW + timedelta(days=61))
    assert "sv/amning" not in published(data)
    info = load_state(data)["subjects"]["sv/amning"]
    assert (info["status"], info["withheld_reason"]) == ("withheld", "stale")


def test_removal_requires_a_complete_listing(seeded, data, tmp_path):
    subjects = seeded.fake.data["subjects"]["sv"]
    subjects[:] = [s for s in subjects if s["namn"] != "amning"]
    subjects.append({"namn": 7})
    result = execute(seeded, data, tmp_path / "incomplete")
    assert result["counts"]["removed"] == 0
    assert "sv/amning" in published(data)

    subjects.pop()
    result = execute(seeded, data, tmp_path / "removed")
    assert result["counts"]["removed"] == 1
    assert "sv/amning" not in published(data)
    assert "sv/amning" not in load_state(data)["subjects"]
    assert "en/dodsorsaker" in published(data)


def test_swedish_listing_failure_publishes_nothing(seeded, data, tmp_path):
    seeded.fake.failing_paths.add("/api/v1/sv")
    before = (data / collection("sv")).read_bytes()
    with pytest.raises(Unreachable):
        start(seeded, data, tmp_path / "out")
    assert (data / collection("sv")).read_bytes() == before


def test_bounded_run_touches_only_selected(seeded, data, tmp_path):
    later = NOW + timedelta(days=1)
    # A bounded run against a partial listing proves no removal.
    seeded.fake.data["subjects"]["sv"] = [
        s for s in SDB_DATA["subjects"]["sv"] if s["namn"] == "amning"
    ]
    execute(
        seeded,
        data,
        tmp_path / "bounded",
        clock=later,
        subjects=["amning"],
        languages=["sv"],
    )
    state = load_state(data)["subjects"]
    assert state["sv/amning"]["checked_at"] == later.isoformat()
    assert state["sv/dodsorsaker"]["checked_at"] == NOW.isoformat()
    assert state["en/dodsorsaker"]["checked_at"] == NOW.isoformat()
    assert len(published(data)) == 4


def test_renamed_result_fields_and_unlisted_years(seeded, tmp_path):
    seeded.fake.result_names["amning"] = {"variabel": "grupp"}
    listed = [str(item["id"]) for item in SDB_DATA["categories"]["sv"]["amning"]["ar"]]
    first = int(listed[0])
    rows = []
    for year in (first - 2, first):
        row = first_codes_row("amning", ar=year)
        row["gruppId"] = row.pop("variabelId")
        rows.append(row)
    seeded.fake.rows["amning"] = rows
    data = tmp_path / "data"
    execute(seeded, data, tmp_path / "out", subjects=["amning"], languages=["sv"])
    document = load_documents(data, LANGUAGES)["sv/amning"]
    native = document["extension"]["socialstyrelsen"]
    assert native["result_dimensions"] == {"variabel": "grupp"}
    assert document["dimension"]["ar"]["category"]["index"][0] == str(first - 2)


def test_stale_artifact_rejected(seeded, data, tmp_path):
    from apps.harvest.metadata_adapters.sdb.workflow import collect

    start(seeded, data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=LANGUAGES)

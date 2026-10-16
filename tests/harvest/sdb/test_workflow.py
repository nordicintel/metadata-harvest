"""Weekly SDB lifecycle against the in-process SDB imitation."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.harvest.metadata_adapters.sdb.client import SdbClient, Unreachable
from apps.harvest.metadata_adapters.sdb.state import PROVIDER_CODE, load_state
from apps.harvest.source_store import SourceStore, harvest_manifest, source_key
from tests.support.binding import binding
from tests.support.parsing import documents

SDB_DATA = json.loads(
    (Path("tests/fixtures/sdb") / "sdb.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 10, 12, 5, tzinfo=UTC)
PROVIDER = binding(
    "sdb", "socialstyrelsen", languages=["sv", "en"], retry_cooldown_seconds=0
)
LANGUAGES = PROVIDER.options["languages"]
MANIFEST = "source/manifest.json"


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
        client = SdbClient(base_url=sdb.api_root, retry_delay=0)
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


def load_documents(data):
    return documents("sdb", data, [PROVIDER])


def published(data):
    return set(load_documents(data))


def store(data):
    return SourceStore(data / "source", "sdb")


def stable(data):
    return {
        k: v for k, v in harvest_manifest(data).items() if k != "reports/latest.json"
    }


@pytest.fixture
def data(seeded, tmp_path):
    root = tmp_path / "data" / "sdb"
    result = execute(seeded, root, tmp_path / "bootstrap")
    assert result["status"] == "success"
    assert result["counts"]["rebuilt"] == 4
    return root


def test_bootstrap_stores_raw_responses_for_both_languages(data):
    from schema.validation import validate_metadata

    assert published(data) == {
        "sv/amning",
        "sv/dodsorsaker",
        "sv/drgstatistikislutenvard",
        "en/dodsorsaker",
    }
    for document in load_documents(data).values():
        validate_metadata(document, adapter="sdb")
        assert document["extension"]["socialstyrelsen"]["source_record"]["namn"]
    english = load_documents(data)["en/dodsorsaker"]
    assert english["extension"]["nordicintel"]["time_unit"] == "annual"
    envelope = store(data).get(source_key(PROVIDER_CODE, "dodsorsaker", "en"))
    assert set(envelope["responses"]) == {
        "variables",
        "categories",
        "resultat",
        "listing_row",
    }
    assert envelope["responses"]["listing_row"]["namn"] == "dodsorsaker"
    shared = store(data).manifest["shared"]
    assert {f"{PROVIDER_CODE}/_shared/listing.{lang}.json" for lang in LANGUAGES} <= (
        set(shared)
    )
    assert not (data / "metadata").exists()


def test_probe_is_shared_between_languages(data):
    probe = store(data).get_shared(PROVIDER_CODE, "probe/dodsorsaker.json")
    assert probe["language"] == "sv"
    assert probe["first"]["data"]
    assert set(probe) == {"language", "first", "years", "candidates"}


def test_rerun_is_byte_identical_and_unchanged(seeded, data, tmp_path):
    before = stable(data)
    manifest = (data / MANIFEST).read_bytes()
    result = execute(seeded, data, tmp_path / "rerun")
    assert result["counts"]["unchanged"] == 4
    assert result["counts"]["rebuilt"] == 0
    assert (data / MANIFEST).read_bytes() == manifest
    assert stable(data) == before


def test_english_listing_failure_withholds_only_english(seeded, data, tmp_path):
    seeded.fake.failing_paths.add("/api/v1/en")
    result = execute(seeded, data, tmp_path / "en-down")
    assert result["status"] == "completed_with_errors"
    assert published(data) == {
        "sv/amning",
        "sv/dodsorsaker",
        "sv/drgstatistikislutenvard",
    }
    state = load_state(data)
    assert "withheld" not in state
    assert state["subjects"]["en/dodsorsaker"]["status"] == "withheld"
    assert store(data).get(source_key(PROVIDER_CODE, "dodsorsaker", "en"))

    seeded.fake.failing_paths.clear()
    result = execute(seeded, data, tmp_path / "en-back")
    assert result["counts"]["restored"] == 1
    assert "en/dodsorsaker" in published(data)


def test_failing_result_endpoint_is_withheld_then_restored(seeded, data, tmp_path):
    before = load_documents(data)["sv/amning"]
    seeded.fake.failing_paths.add("/api/v1/sv/amning/resultat")
    seeded.fake.requests.clear()
    result = execute(seeded, data, tmp_path / "down")
    assert result["counts"]["withheld"] == 1
    assert "sv/amning" not in published(data)
    state = load_state(data)
    assert state["subjects"]["sv/amning"]["status"] == "withheld"
    assert state["subjects"]["sv/amning"]["error"]["stage"] == "endpoint"
    assert documents("sdb", data, [PROVIDER], include_withheld=True)["sv/amning"] == (
        before
    )
    probes = seeded.fake.requests.count("/api/v1/sv/amning/resultat?per_sida=1")
    assert probes == 6  # three attempts, then one end-of-run retry pass

    seeded.fake.failing_paths.clear()
    result = execute(seeded, data, tmp_path / "back")
    assert result["counts"]["restored"] == 1
    assert load_documents(data)["sv/amning"] == before


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
    assert source_key(PROVIDER_CODE, "amning", "sv") not in store(data).entries()
    assert store(data).get_shared(PROVIDER_CODE, "probe/amning.json") is None
    assert "en/dodsorsaker" in published(data)


def test_swedish_listing_failure_publishes_nothing(seeded, data, tmp_path):
    seeded.fake.failing_paths.add("/api/v1/sv")
    before = harvest_manifest(data)
    with pytest.raises(Unreachable):
        start(seeded, data, tmp_path / "out")
    assert harvest_manifest(data) == before


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
    assert store(data).get_shared(PROVIDER_CODE, "listing.en.json")


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
    data = tmp_path / "data" / "sdb"
    execute(seeded, data, tmp_path / "out", subjects=["amning"], languages=["sv"])
    probe = store(data).get_shared(PROVIDER_CODE, "probe/amning.json")
    assert probe["years"][str(first - 1)] is None
    assert str(first - 2) not in probe["years"]  # observed in the first row
    assert probe["candidates"][:2] == [first - 2, first - 1]
    document = load_documents(data)["sv/amning"]
    native = document["extension"]["socialstyrelsen"]
    assert native["result_dimensions"] == {"variabel": "grupp"}
    assert document["dimension"]["ar"]["category"]["index"][0] == str(first - 2)


def test_stale_artifact_rejected(seeded, data, tmp_path):
    from apps.harvest.metadata_adapters.sdb.workflow import collect

    start(seeded, data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=LANGUAGES)

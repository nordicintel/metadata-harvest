"""Försäkringskassan lifecycle against recorded (and labelled synthetic) responses."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from apps.harvest.metadata_adapters.forsakringskassan.api import (
    REPORTS_URL,
    TEXTS_URL,
    meta_url,
)
from apps.harvest.metadata_adapters.forsakringskassan.client import Unreachable
from apps.harvest.metadata_adapters.forsakringskassan.state import (
    PROVIDER_CODE,
    load_state,
)
from apps.harvest.source_store import SourceStore, harvest_manifest, source_key
from tests.support.forsakringskassan import RecordedClient
from tests.support.parsing import documents

NOW = datetime(2026, 10, 13, 5, tzinfo=UTC)
PROVIDER = {
    "code": "forsakringskassan",
    "languages": ["sv"],
    "retry_cooldown_seconds": 0,
    "max_age_days": 60,
}
ALL_KEYS = {
    "sv/ase-avslutsanledning/ASEAvslutsanledning",
    "sv/ase-beslut-lan/ASEBeslutKon",
    "sv/ase-beslut-lan/ASEBeslutLan",
    "sv/syn-alias/SynAliasLan",
    "sv/syn-kommun/SynKommunKon",
}


def start(source, data, output, *, clock=NOW, **options):
    from apps.harvest.metadata_adapters.forsakringskassan.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from apps.harvest.metadata_adapters.forsakringskassan.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data, *, include_withheld=False):
    return documents(
        "forsakringskassan", data, [PROVIDER], include_withheld=include_withheld
    )


def stored_key(key):
    language, code = key.split("/", 1)
    return source_key(PROVIDER_CODE, code, language)


def store(data):
    return SourceStore(data / "source", "forsakringskassan")


def stable(data):
    return {
        k: v for k, v in harvest_manifest(data).items() if k != "reports/latest.json"
    }


MANIFEST = "source/manifest.json"


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data" / "forsakringskassan"
    result = execute(RecordedClient(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == 5
    return root


def test_bootstrap_stores_listing_rows_meta_and_probes(data, tmp_path):
    from apps.harvest.validation import validate_metadata

    parsed = published(data)
    assert set(parsed) == ALL_KEYS
    for document in parsed.values():
        validate_metadata(document, adapter="forsakringskassan")
    report = load_state(data)["reports"]
    assert report["syn-kommun"]["undeclared"] == {"SynKommunLan": ["kommun_kod"]}
    assert len(report["ase-beslut-lan"]["meta_sha256"]) == 64
    assert "withheld" not in load_state(data)
    plain = store(data).get(stored_key("sv/ase-beslut-lan/ASEBeslutLan"))
    assert set(plain["responses"]) == {"listing"}
    assert plain["responses"]["listing"]["report"] == "ase-beslut-lan"
    assert plain["derived"]["table"]["name"] == "ASEBeslutLan"
    probed = store(data).get(stored_key("sv/syn-kommun/SynKommunKon"))
    assert set(probed["responses"]) == {"listing", "probe"}
    shared = set(store(data).manifest["shared"])
    assert {
        f"{PROVIDER_CODE}/_shared/reports.json",
        f"{PROVIDER_CODE}/_shared/texts.json",
        f"{PROVIDER_CODE}/_shared/meta/ase-beslut-lan.json",
    } <= shared
    assert stored_key("sv/syn-kommun/SynKommunLan") not in store(data).entries()
    assert not (data / "metadata").exists()


def test_undeclared_dimension_table_is_excluded_not_failed(tmp_path):
    result = execute(RecordedClient(), tmp_path / "data", tmp_path / "out")
    assert result["counts"]["excluded"] == 1
    assert result["undeclared_dimensions"] == {
        "syn-kommun/SynKommunLan": ["kommun_kod"]
    }
    assert result["errors"] == []


def test_rerun_is_byte_identical_and_skips_unchanged_reports(data, tmp_path):
    before = stable(data)
    manifest = (data / MANIFEST).read_bytes()
    source = RecordedClient()
    result = execute(source, data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 5
    assert result["counts"]["excluded"] == 1
    assert (data / MANIFEST).read_bytes() == manifest
    assert stable(data) == before
    # /reports, texts and four meta.json: no probes for unchanged reports.
    assert source.request_count == 6


def test_full_run_rebuilds_everything_byte_identically(data, tmp_path):
    before = stable(data)
    source = RecordedClient()
    result = execute(source, data, tmp_path / "full", full=True)
    assert result["counts"]["unchanged"] == 5 and result["counts"]["rebuilt"] == 0
    assert source.request_count == 8  # plus the two syn-kommun probes
    assert result["selection"]["full"] is True
    assert stable(data) == before


def test_changed_meta_rebuilds_only_its_report(data, tmp_path):
    source = RecordedClient()
    meta = source.body(meta_url("ase-avslutsanledning"))
    meta["filter"]["dimension"][0]["values"].insert(
        0, {"key": "2026", "label": "2026", "selected": True}
    )
    source.respond(meta_url("ase-avslutsanledning"), meta)
    result = execute(source, data, tmp_path / "changed")
    assert result["counts"]["rebuilt"] == 1 and result["counts"]["unchanged"] == 4
    document = published(data)["sv/ase-avslutsanledning/ASEAvslutsanledning"]
    assert document["extension"]["nordicintel"]["last_period"] == "2026"
    stored = store(data).get_shared(PROVIDER_CODE, "meta/ase-avslutsanledning.json")
    assert stored["filter"]["dimension"][0]["values"][0]["key"] == "2026"


def test_changed_texts_rebuild_only_affected_reports(data, tmp_path):
    source = RecordedClient()
    texts = source.body(TEXTS_URL)
    texts["MEKA.report.syn-alias"]["name"] = "Nytt namn"
    source.respond(TEXTS_URL, texts)
    result = execute(source, data, tmp_path / "texts")
    assert result["counts"]["rebuilt"] == 1
    assert published(data)["sv/syn-alias/SynAliasLan"]["label"] == "Nytt namn"


def test_failing_meta_is_retried_withheld_then_restored(data, tmp_path):
    before = {k: d for k, d in published(data).items() if "/ase-beslut-lan/" in k}
    source = RecordedClient()
    source.fail(meta_url("ase-beslut-lan"))
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("endpoint", "*/ase-beslut-lan")
    ]
    assert result["errors"][0]["url"] == meta_url("ase-beslut-lan")
    assert source.requests.count(f"GET {meta_url('ase-beslut-lan')}") == 2  # retried
    assert result["counts"]["withheld"] == 2
    assert not any("/ase-beslut-lan/" in key for key in published(data))
    state = load_state(data)["datasets"]
    assert {state[k]["status"] for k in before} == {"withheld"}
    kept = published(data, include_withheld=True)
    assert {k: kept[k] for k in before} == before
    assert len(published(data)) == 3  # siblings still published

    result = execute(RecordedClient(), data, tmp_path / "back")
    assert result["counts"]["restored"] == 2
    assert {k: d for k, d in published(data).items() if "/ase-beslut-lan/" in k} == (
        before
    )


def test_texts_without_a_report_name_are_stored_and_fail_at_parse(data, tmp_path):
    from apps.parsing.build import build

    before = published(data)
    source = RecordedClient()
    texts = source.body(TEXTS_URL)
    del texts["MEKA.report.ase-avslutsanledning"]
    source.respond(TEXTS_URL, texts)
    result = execute(source, data, tmp_path / "bad")
    assert result["status"] == "success"  # the harvest stores what was returned
    parsed = build(
        "forsakringskassan", data.parent, tmp_path / "parsed", providers=[PROVIDER]
    )
    assert [e["key"] for e in parsed["errors"]] == [
        stored_key("sv/ase-avslutsanledning/ASEAvslutsanledning")
    ]
    assert parsed["counts"]["parsed"] == 4

    result = execute(RecordedClient(), data, tmp_path / "fixed")
    assert result["errors"] == []
    assert published(data) == before


def test_texts_failure_is_a_dependency_failure(data, tmp_path):
    before = stable(data)
    source = RecordedClient()
    source.fail(TEXTS_URL)
    result = execute(source, data, tmp_path / "texts-down")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "texts")
    ]
    assert result["counts"]["rebuilt"] == result["counts"]["unchanged"] == 0
    assert stable(data) == before

    later = NOW + timedelta(days=61)
    result = execute(source, data, tmp_path / "texts-stale", clock=later)
    assert published(data) == {}
    assert result["counts"]["withheld"] == 5
    assert {
        info["withheld_reason"] for info in load_state(data)["datasets"].values()
    } == {"stale"}


def test_malformed_listing_row_before_valid_rows(data, tmp_path):
    source = RecordedClient()
    listing = source.body(REPORTS_URL)
    listing.insert(0, {"area": "fn", "reports": ["no-key"]})  # synthetic
    listing[2]["reports"].remove("syn-alias")  # absent, but listing incomplete
    source.respond(REPORTS_URL, listing)
    result = execute(source, data, tmp_path / "malformed")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "reports/#0")
    ]
    assert result["counts"]["unchanged"] == 4 and result["counts"]["removed"] == 0
    assert set(published(data)) == ALL_KEYS


def test_listing_failure_raises_and_publishes_nothing(data, tmp_path):
    before = harvest_manifest(data)
    source = RecordedClient()
    source.fail(REPORTS_URL)
    with pytest.raises(Unreachable):
        start(source, data, tmp_path / "out")
    assert harvest_manifest(data) == before
    source = RecordedClient()
    source.respond(REPORTS_URL, {"error": "maintenance"})
    with pytest.raises(ValueError):
        start(source, data, tmp_path / "out2")


def test_removal_only_in_unbounded_run(data, tmp_path):
    source = RecordedClient()
    listing = source.body(REPORTS_URL)
    listing[1]["reports"].remove("syn-alias")
    source.respond(REPORTS_URL, listing)
    meta = source.body(meta_url("ase-beslut-lan"))
    meta["filter"]["uppdelning"].pop()  # ASEBeslutLan no longer listed
    source.respond(meta_url("ase-beslut-lan"), meta)

    result = execute(source, data, tmp_path / "bounded", subjects=["ase-beslut-lan"])
    assert result["counts"]["removed"] == 0
    assert set(published(data)) == ALL_KEYS

    result = execute(source, data, tmp_path / "unbounded")
    assert result["counts"]["removed"] == 2
    assert set(published(data)) == ALL_KEYS - {
        "sv/syn-alias/SynAliasLan",
        "sv/ase-beslut-lan/ASEBeslutLan",
    }
    assert "syn-alias" not in load_state(data)["reports"]
    assert stored_key("sv/syn-alias/SynAliasLan") not in store(data).entries()
    assert store(data).get_shared(PROVIDER_CODE, "meta/syn-alias.json") is None


def test_bounded_run_touches_only_selected(data, tmp_path):
    later = NOW + timedelta(days=1)
    source = RecordedClient()
    result = execute(
        source, data, tmp_path / "bounded", clock=later, subjects=["syn-alias"]
    )
    assert result["selection"]["subjects"] == ["syn-alias"]
    assert source.request_count == 3  # /reports, texts, one meta.json
    state = load_state(data)["datasets"]
    assert state["sv/syn-alias/SynAliasLan"]["checked_at"] == later.isoformat()
    assert {
        info["checked_at"] for key, info in state.items() if "syn-alias" not in key
    } == {NOW.isoformat()}


def test_unknown_subject_is_reported(data, tmp_path):
    result = execute(RecordedClient(), data, tmp_path / "x", subjects=["nope"])
    assert result["errors"][0]["resource"] == "nope"
    assert "Unknown" in result["errors"][0]["error"]


def test_endpoint_telemetry_contains_the_report_url(data, tmp_path, monkeypatch):
    from apps.harvest.metadata_adapters.forsakringskassan import workflow

    events = []

    def error(self, stage, resource, language, message, **details):
        events.append({"stage": stage, "resource": resource, **details})

    monkeypatch.setattr(workflow.Progress, "error", error)
    source = RecordedClient()
    source.fail(meta_url("ase-beslut-lan"))
    start(source, data, tmp_path / "endpoint")
    assert events == [
        {
            "stage": "endpoint",
            "resource": "*/ase-beslut-lan",
            "url": meta_url("ase-beslut-lan"),
        }
    ]


def test_duplicates_are_quarantined(data, tmp_path):
    before = published(data)
    source = RecordedClient()
    listing = source.body(REPORTS_URL)
    listing[1]["reports"].append("ase-avslutsanledning")  # listed twice
    source.respond(REPORTS_URL, listing)
    meta = source.body(meta_url("ase-beslut-lan"))
    meta["filter"]["uppdelning"].append(dict(meta["filter"]["uppdelning"][1]))
    source.respond(meta_url("ase-beslut-lan"), meta)

    result = execute(source, data, tmp_path / "dup")
    resources = [(e["stage"], e["resource"]) for e in result["errors"]]
    assert resources == [
        ("discovery", "reports/ase-avslutsanledning"),
        ("metadata", "sv/ase-beslut-lan/ASEBeslutLan"),
    ]
    assert f"GET {meta_url('ase-avslutsanledning')}" not in source.requests
    after = published(data)
    assert set(after) == set(before)  # nothing removed
    key = "sv/ase-avslutsanledning/ASEAvslutsanledning"
    assert after[key] == before[key]  # quarantined by discovery: meta not refetched
    key = "sv/ase-beslut-lan/ASEBeslutLan"
    # Quarantined in meta.json: still parsed, from its stored table definition
    # against the current meta.json (whose hash, table list and single-table
    # labelling it now reflects); its structure is unchanged.
    changed, kept = after[key], before[key]
    assert changed["id"] == kept["id"] and changed["dimension"] == kept["dimension"]
    assert changed["extension"]["nordicintel"] == kept["extension"]["nordicintel"]
    assert result["counts"]["rebuilt"] == 1  # the sibling ASEBeslutKon (meta changed)


def test_stale_artifact_rejected(data, tmp_path):
    from apps.harvest.metadata_adapters.forsakringskassan.workflow import collect

    start(RecordedClient(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])


def test_output_inside_data_and_english_are_rejected(data, tmp_path):
    with pytest.raises(ValueError, match="separate"):
        start(RecordedClient(), data, data / "out")
    with pytest.raises(ValueError, match="Swedish"):
        start(RecordedClient(), data, tmp_path / "en", languages=["en"])

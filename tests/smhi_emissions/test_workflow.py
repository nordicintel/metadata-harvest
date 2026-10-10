"""Monthly SMHI emissions lifecycle against recorded and labelled synthetic responses."""

import asyncio
import copy
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harvesting.adapters.smhi_emissions.api import FILTER_OPTIONS_URL
from harvesting.adapters.smhi_emissions.client import Unreachable
from harvesting.adapters.smhi_emissions.state import (
    collection,
    load_documents,
    load_state,
)
from tests.smhi_emissions.conftest import (
    RecordedClient,
    fixture_key,
    probe_key,
    substances,
)

NOW = datetime(2026, 11, 3, 7, 30, tzinfo=UTC)
PROVIDER = {"code": "smhi", "languages": ["sv"], "retry_cooldown_seconds": 0}
LISTING = fixture_key(FILTER_OPTIONS_URL)


class Source(RecordedClient):
    """Recorded answers; failing or unrecorded requests are unreachable, like
    ``SmhiClient`` after its retries."""

    def _answer(self, url, params):
        try:
            return super()._answer(url, params)
        except (TimeoutError, LookupError) as error:
            raise Unreachable(str(error)) from error


def start(source, data, output, *, clock=NOW, **options):
    from harvesting.adapters.smhi_emissions.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from harvesting.adapters.smhi_emissions.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data):
    return load_documents(data, ["sv"])


def key(code):
    return f"sv/emissionsdatabasen_{code}"


@pytest.fixture(scope="module")
def bootstrapped(tmp_path_factory):
    base = tmp_path_factory.mktemp("bootstrap")
    root = base / "data"
    result = execute(Source(), root, base / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(published(root)) == 29
    return root


@pytest.fixture
def data(bootstrapped, tmp_path):
    """A private copy of the bootstrapped published data."""
    return Path(shutil.copytree(bootstrapped, tmp_path / "data"))


def test_bootstrap_publishes_one_valid_dataset_per_substance(data):
    from harvesting.validation import validate_metadata

    documents = published(data)
    assert set(documents) == {key(code) for code in substances()}
    for document in documents.values():
        validate_metadata(document, adapter="smhi_emissions")
    assert load_state(data)["datasets"][key("CO2")]["submission"] == 2026


def test_bootstrap_requests_listing_then_one_probe_per_substance(tmp_path):
    source = Source()
    start(source, tmp_path / "data", tmp_path / "out")
    assert source.requests == [LISTING, *(probe_key(code) for code in substances())]


def test_rerun_is_byte_identical_and_unchanged(data, tmp_path):
    before = (data / collection("sv")).read_bytes()
    result = execute(Source(), data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == 29
    assert (data / collection("sv")).read_bytes() == before


def test_failing_probe_is_withheld_then_restored(data, tmp_path):
    """Synthetic: the NOx probe times out on both attempts."""
    before = published(data)[key("NOx")]
    source = Source()
    source.failing = {probe_key("NOx")}
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert source.requests.count(probe_key("NOx")) == 2  # retried once
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("endpoint", key("NOx"))
    ]
    assert result["errors"][0]["url"].endswith("sub=NOx")
    assert result["counts"]["withheld"] == 1
    assert key("NOx") not in published(data)
    assert load_state(data)["withheld"][key("NOx")] == before

    result = execute(Source(), data, tmp_path / "back")
    assert result["counts"]["restored"] == 1
    assert published(data)[key("NOx")] == before


def test_unknown_sector_label_keeps_document_until_stale(data, tmp_path):
    """Synthetic: the CO2 probe names a sector the tree does not have."""
    before = published(data)
    source = Source()
    probe = source.responses[probe_key("CO2")]["data"]["Koldioxid (CO2)"]
    probe["Rymdfart"] = probe["Transporter"]
    result = execute(source, data, tmp_path / "unknown-label")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("metadata", key("CO2"))
    ]
    assert "Rymdfart" in result["errors"][0]["error"]
    assert published(data) == before

    later = NOW + timedelta(days=61)
    result = execute(source, data, tmp_path / "stale", clock=later)
    assert key("CO2") not in published(data)
    assert load_state(data)["datasets"][key("CO2")]["withheld_reason"] == "stale"
    assert result["counts"]["withheld"] == 1


def test_malformed_substance_row_before_later_valid_rows(data, tmp_path):
    """Synthetic: the first substance entry of the first subsector is broken."""
    source = Source()
    source.tree[0]["subsectors"][0]["substances"].insert(0, {"Zz": "broken"})
    source.tree[1]["subsectors"][0]["substances"].insert(0, ["not", "an", "object"])
    result = execute(source, data, tmp_path / "malformed")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "filteroptions/1/0"),
        ("discovery", "filteroptions/2.3/0"),
    ]
    assert result["counts"]["unchanged"] == 29
    assert result["tree_complete"] is False
    assert len(published(data)) == 29


def test_listing_failure_publishes_nothing(data, tmp_path):
    source = Source()
    source.failing = {LISTING}
    before = (data / collection("sv")).read_bytes()
    with pytest.raises(Unreachable):
        start(source, data, tmp_path / "out")
    assert (data / collection("sv")).read_bytes() == before
    assert not (tmp_path / "out" / "result.json").exists()


@pytest.mark.parametrize("malformed", [False, True])
def test_discovery_failure_is_reported_live_before_raise(
    data, tmp_path, monkeypatch, malformed
):
    from harvesting.adapters.smhi_emissions import workflow

    events = []
    monkeypatch.setattr(
        workflow.Progress,
        "error",
        lambda self, stage, resource, language, error, **details: events.append(
            {"stage": stage, "resource": resource, **details}
        ),
    )
    source = Source()
    if malformed:
        source.tree[3]["subsectors"][0]["code"] = "7.1"
    else:
        source.failing = {LISTING}
    with pytest.raises(ValueError if malformed else Unreachable):
        start(source, data, tmp_path / "out")
    assert events == [
        {"stage": "discovery", "resource": "filteroptions", "url": FILTER_OPTIONS_URL}
    ]
    assert not (tmp_path / "out" / "result.json").exists()


@pytest.mark.parametrize("stage", ["endpoint", "metadata"])
def test_failed_probe_is_reported_before_valid_sibling(
    data, tmp_path, monkeypatch, stage
):
    from harvesting.adapters.smhi_emissions import workflow

    events, before_sibling = [], []
    monkeypatch.setattr(
        workflow.Progress,
        "error",
        lambda self, event_stage, resource, language, error, **details: events.append(
            {"stage": event_stage, "resource": resource, **details}
        ),
    )

    class Observed(Source):
        def _answer(self, url, params):
            if fixture_key(url, params) == probe_key("NOx"):
                before_sibling.append(copy.deepcopy(events))
            return super()._answer(url, params)

    source = Observed()
    if stage == "endpoint":
        source.failing = {probe_key("CO2")}
    else:
        source.responses[probe_key("CO2")]["data"]["Koldioxid (CO2)"]["Unknown"] = []
    result = start(source, data, tmp_path / "out", subjects=["CO2", "NOx"])
    assert before_sibling and before_sibling[0][0]["resource"] == key("CO2")
    assert before_sibling[0][0]["url"].endswith("sub=CO2")
    assert result["errors"][0]["stage"] == stage
    assert len(events) == (2 if stage == "endpoint" else 1)
    assert events[0]["attempt"] == 1
    assert events[0]["retrying"] is (stage == "endpoint")


def test_recovered_retry_has_live_attempt_but_no_final_error(
    data, tmp_path, monkeypatch
):
    from harvesting.adapters.smhi_emissions import workflow

    events = []
    monkeypatch.setattr(
        workflow.Progress,
        "error",
        lambda self, stage, resource, language, error, **details: events.append(
            details
        ),
    )

    class Transient(Source):
        failed = False

        def _answer(self, url, params):
            if fixture_key(url, params) == probe_key("CO2") and not self.failed:
                self.failed = True
                raise Unreachable("temporary outage")
            return super()._answer(url, params)

    result = start(Transient(), data, tmp_path / "out", subjects=["CO2"])
    assert result["status"] == "success"
    assert result["errors"] == []
    assert result["counts"]["failed"] == 0
    assert len(events) == 1
    assert events[0]["attempt"] == 1 and events[0]["retrying"] is True
    assert events[0]["url"].endswith("sub=CO2")


def test_malformed_sector_tree_publishes_nothing(data, tmp_path):
    """Synthetic: a subsector code outside its sector makes the tree unusable."""
    source = Source()
    source.tree[3]["subsectors"][0]["code"] = "7.1"
    with pytest.raises(ValueError, match="not under sector 4"):
        start(source, data, tmp_path / "out")
    assert not (tmp_path / "out" / "result.json").exists()


def test_removal_only_in_an_unbounded_run_with_a_complete_tree(data, tmp_path):
    """Synthetic: Se disappears from every subsector."""
    source = Source()
    for sector in source.tree:
        for subsector in sector["subsectors"]:
            subsector["substances"] = [
                e for e in subsector["substances"] if "Se" not in e
            ]
    result = execute(source, data, tmp_path / "bounded", subjects=["CO2"])
    assert result["counts"]["removed"] == 0 and key("Se") in published(data)

    incomplete = Source()
    incomplete.responses[LISTING] = copy.deepcopy(source.tree)
    incomplete.tree[0]["subsectors"][0]["substances"].append({"Xx": None})
    result = execute(incomplete, data, tmp_path / "incomplete")
    assert result["counts"]["removed"] == 0 and key("Se") in published(data)

    result = execute(source, data, tmp_path / "unbounded")
    assert result["counts"]["removed"] == 1
    assert key("Se") not in published(data)
    assert key("Se") not in load_state(data)["datasets"]


def test_bounded_run_touches_only_selected(data, tmp_path):
    later = NOW + timedelta(days=1)
    source = Source()
    result = execute(source, data, tmp_path / "bounded", clock=later, subjects=["co2"])
    assert source.requests == [LISTING, probe_key("CO2")]
    assert result["selection"]["subjects"] == ["co2"]
    state = load_state(data)["datasets"]
    assert state[key("CO2")]["checked_at"] == later.isoformat()
    assert {i["checked_at"] for k, i in state.items() if k != key("CO2")} == {
        NOW.isoformat()
    }


def test_unknown_subject_is_reported(data, tmp_path):
    result = execute(Source(), data, tmp_path / "unknown", subjects=["XYZ"])
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("discovery", "*/XYZ")
    ]
    assert "Unknown" in result["errors"][0]["error"]


def test_conflicting_duplicate_substance_is_quarantined(data, tmp_path):
    """Synthetic: CO2 is listed once more with another substance_id."""
    before = published(data)[key("CO2")]
    source = Source()
    source.tree[3]["subsectors"][0]["substances"].append(
        {"CO2": {"substance_id": 99, "long_name": "Koldioxid (CO2)"}}
    )
    result = execute(source, data, tmp_path / "duplicate")
    assert [(e["stage"], e["resource"]) for e in result["errors"]] == [
        ("metadata", key("CO2"))
    ]
    assert "conflicting" in result["errors"][0]["error"]
    assert probe_key("CO2") not in source.requests
    assert published(data)[key("CO2")] == before  # kept, not replaced
    assert result["counts"]["removed"] == 0


def test_stale_artifact_rejected(data, tmp_path):
    from harvesting.adapters.smhi_emissions.workflow import collect

    start(Source(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])


def test_english_is_not_configurable(tmp_path):
    from harvesting.adapters.smhi_emissions.workflow import run

    with pytest.raises(ValueError, match="Swedish only"):
        asyncio.run(
            run(
                {**PROVIDER, "languages": ["sv", "en"]},
                tmp_path / "data",
                tmp_path / "out",
                client=Source(),
            )
        )

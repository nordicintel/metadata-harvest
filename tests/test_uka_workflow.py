"""Weekly UKÄ lifecycle against recorded indicators 67 and 72."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from metadata_harvest.adapters.uka.client import ExportFile, Unreachable
from metadata_harvest.adapters.uka.state import (
    COLLECTION,
    load_documents,
    load_state,
)
from tests.uka_support import RECORDED, UKA, export_text

NOW = datetime(2026, 10, 12, 5, tzinfo=UTC)
PROVIDER = {"code": "uka", "retry_cooldown_seconds": 0}


def link_key(indicator, period):
    query = urlencode(
        sorted({"export": "all", "export_type": "csv", "year": period}.items())
    )
    return f"/api/totals/{indicator}?{query}"


class Source:
    """In-memory UKÄ answering like UkaClient from the recordings."""

    def __init__(self, ids=(67, 72)):
        responses = RECORDED["responses"]
        self.body = {
            group: [item for item in items if item["id"] in ids]
            for group, items in responses["/api/indicators"].items()
        }
        self.body = {group: items for group, items in self.body.items() if items}
        self.totals_bodies = {i: responses[f"/api/totals/{i}"] for i in ids}
        self.links = {}
        for i in ids:
            for item in self.totals_bodies[i]["yearly_totals"]["totals"]:
                period = str(item["year"]).strip()
                self.links[(i, period)] = responses[link_key(i, period)]["url"]
        self.files = {}
        for path, entry in RECORDED["files"].items():
            text = export_text(entry["file"].removesuffix(".gz"))
            self.files[UKA + path] = (text, entry["last_modified"])
        self.down = set()
        self.catalogue_error = None
        self.downloads, self.heads, self.totals_calls = [], [], []

    async def catalogue(self):
        if self.catalogue_error:
            raise self.catalogue_error
        return copy.deepcopy(self.body)

    async def totals(self, indicator):
        self.totals_calls.append(indicator)
        if indicator in self.down:
            raise Unreachable(f"totals {indicator} timed out")
        return copy.deepcopy(self.totals_bodies[indicator])

    async def export_link(self, indicator, period):
        if indicator in self.down:
            raise Unreachable("link timed out")
        return self.links.get((indicator, period))

    async def export(self, url):
        self.downloads.append(url)
        if url not in self.files:
            return None
        text, modified = self.files[url]
        return ExportFile(url, text, modified)

    async def available(self, url):
        self.heads.append(url)
        return url in self.files

    def replace_newest(self, indicator, text=None):
        """Give the newest period a new upload URL (and optionally new text)."""
        period = max(p for i, p in self.links if i == indicator)
        old = self.links[(indicator, period)]
        new = old.replace("-v1.csv", "-v2.csv")
        content, modified = self.files[old]
        self.files[new] = (text if text is not None else content, modified)
        self.links[(indicator, period)] = new
        return new


def execute(source, data, output, *, clock=NOW, **options):
    from metadata_harvest.adapters.uka.workflow import collect, run

    result = asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )
    collect(data, output)
    return result


@pytest.fixture
def data(tmp_path):
    """Published data after a bootstrap run from an empty directory."""
    root = tmp_path / "data"
    result = execute(Source(), root, tmp_path / "bootstrap")
    assert result["status"] == "success"
    assert result["counts"]["rebuilt"] == 2
    return root


def test_bootstrap_publishes_valid_documents(data):
    from metadata_harvest.validation import validate_metadata

    documents = load_documents(data)
    assert set(documents) == {"67", "72"}
    for document in documents.values():
        validate_metadata(document, adapter="uka")
        assert document["extension"]["uka"]["source_record"]["catalogue"]["id"]
    state = load_state(data)
    assert {i["status"] for i in state["indicators"].values()} == {"published"}
    assert state["withheld"] == {}


def test_unchanged_rerun_downloads_nothing_and_is_byte_identical(
    data, tmp_path, monkeypatch
):
    before = (data / COLLECTION).read_bytes()
    monkeypatch.setattr(
        "metadata_harvest.adapters.uka.workflow.validate_metadata",
        lambda *a, **k: pytest.fail("Unchanged indicators must not be rebuilt"),
    )
    source = Source()
    result = execute(source, data, tmp_path / "rerun")
    assert result["counts"]["reused"] == 2
    assert source.downloads == []
    assert len(source.heads) == 2
    assert (data / COLLECTION).read_bytes() == before


def test_changed_link_rebuilds_only_that_indicator(data, tmp_path):
    source = Source()
    new = source.replace_newest(72)
    result = execute(source, data, tmp_path / "changed")
    assert result["counts"] == {**result["counts"], "rebuilt": 1, "reused": 1}
    assert new in source.downloads
    assert all("andel-disputerade" not in url for url in source.downloads)  # 67
    files = load_documents(data)["72"]["extension"]["uka"]["files"]
    assert new in {file["url"] for file in files.values()}


def test_mapping_version_and_full_rebuild_everything(data, tmp_path, monkeypatch):
    result = execute(Source(), data, tmp_path / "full", full=True)
    assert result["counts"]["rebuilt"] == 2
    monkeypatch.setattr("metadata_harvest.adapters.uka.workflow.MAPPING_VERSION", 99)
    result = execute(Source(), data, tmp_path / "mapping")
    assert result["counts"]["rebuilt"] == 2


def test_unreachable_indicator_is_withheld_then_restored(data, tmp_path):
    before = load_documents(data)["72"]
    source = Source()
    source.down = {72}
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert source.totals_calls.count(72) == 2  # one end-of-run retry pass
    assert result["counts"]["withheld"] == 1
    assert set(load_documents(data)) == {"67"}
    state = load_state(data)
    assert state["withheld"]["72"] == before
    assert state["indicators"]["72"]["status"] == "withheld"
    assert state["indicators"]["72"]["error"]["stage"] == "endpoint"

    source = Source()
    result = execute(source, data, tmp_path / "recovered")
    assert result["status"] == "success"
    assert result["counts"]["restored"] == 1
    assert source.downloads == []
    assert load_documents(data)["72"] == before
    assert load_state(data)["withheld"] == {}


def test_transient_outage_recovered_by_retry_pass(data, tmp_path):
    source = Source()
    calls = 0
    original = source.totals

    async def flaky(indicator):
        nonlocal calls
        if indicator == 72 and calls == 0:
            calls += 1
            raise Unreachable("one timeout")
        return await original(indicator)

    source.totals = flaky
    result = execute(source, data, tmp_path / "flaky")
    assert result["status"] == "success"
    assert set(load_documents(data)) == {"67", "72"}


def test_processing_failure_keeps_document_until_it_is_stale(data, tmp_path):
    before = load_documents(data)["67"]
    source = Source()
    source.replace_newest(67, text="not;a\nvalid;export;row\n")
    result = execute(source, data, tmp_path / "broken")
    assert result["status"] == "completed_with_errors"
    assert result["errors"][0]["stage"] == "metadata"
    assert load_documents(data)["67"] == before  # still published, not advanced

    later = NOW + timedelta(days=61)
    result = execute(source, data, tmp_path / "stale", clock=later)
    assert set(load_documents(data)) == {"72"}
    info = load_state(data)["indicators"]["67"]
    assert (info["status"], info["withheld_reason"]) == ("withheld", "stale")


def test_complete_catalogue_removes_and_incomplete_catalogue_keeps(data, tmp_path):
    source = Source(ids=(67,))
    source.body["Broken"] = [{"name": "row without id"}]
    result = execute(source, data, tmp_path / "incomplete")
    assert result["counts"]["removed"] == 0
    assert set(load_documents(data)) == {"67", "72"}

    result = execute(Source(ids=(67,)), data, tmp_path / "removed")
    assert result["counts"]["removed"] == 1
    assert set(load_documents(data)) == {"67"}
    assert "72" not in load_state(data)["indicators"]


def test_catalogue_failure_publishes_nothing(data, tmp_path):
    from metadata_harvest.adapters.uka.workflow import run

    source = Source()
    source.catalogue_error = Unreachable("UKÄ is down")
    before = (data / COLLECTION).read_bytes()
    with pytest.raises(Unreachable):
        asyncio.run(run(PROVIDER, data, tmp_path / "out", client=source, clock=NOW))
    assert (data / COLLECTION).read_bytes() == before


def test_bounded_run_touches_only_selected(data, tmp_path):
    source = Source(ids=(67,))  # 72 missing, but a bounded run proves nothing
    later = NOW + timedelta(days=1)
    execute(source, data, tmp_path / "bounded", clock=later, indicators=["67"])
    state = load_state(data)
    assert state["indicators"]["67"]["checked_at"] == later.isoformat()
    assert state["indicators"]["72"]["checked_at"] == NOW.isoformat()
    assert set(load_documents(data)) == {"67", "72"}


def test_stale_artifact_rejected(data, tmp_path):
    from metadata_harvest.adapters.uka.workflow import collect, run

    asyncio.run(run(PROVIDER, data, tmp_path / "out", client=Source(), clock=NOW))
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out")

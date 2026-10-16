"""Weekly UKÄ lifecycle against recorded indicators 67 and 72."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from apps.harvest.metadata_adapters.uka.client import ExportFile, Unreachable
from apps.harvest.metadata_adapters.uka.state import load_state
from apps.harvest.source_store import SourceStore, harvest_manifest
from tests.support.parsing import documents
from tests.support.uka_support import RECORDED, UKA, export_text

NOW = datetime(2026, 10, 12, 5, tzinfo=UTC)
PROVIDER = {"code": "uka", "retry_cooldown_seconds": 0}
MANIFEST = "source/manifest.json"


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
    from apps.harvest.metadata_adapters.uka.workflow import collect, run

    result = asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )
    collect(data, output)
    return result


def load_documents(data, *, include_withheld=False):
    """Parsed documents keyed by indicator id."""
    parsed = documents("uka", data, [PROVIDER], include_withheld=include_withheld)
    return {key.split("/", 1)[1]: document for key, document in parsed.items()}


def store(data):
    return SourceStore(data / "source", "uka")


def stable(data):
    return {
        k: v for k, v in harvest_manifest(data).items() if k != "reports/latest.json"
    }


@pytest.fixture
def data(tmp_path):
    """Published data after a bootstrap run from an empty directory."""
    root = tmp_path / "data" / "uka"
    result = execute(Source(), root, tmp_path / "bootstrap")
    assert result["status"] == "success"
    assert result["counts"]["rebuilt"] == 2
    return root


def test_bootstrap_stores_links_and_scan_summaries_not_exports(data):
    from apps.harvest.validation import validate_metadata

    parsed = load_documents(data)
    assert set(parsed) == {"67", "72"}
    for document in parsed.values():
        validate_metadata(document, adapter="uka")
        assert document["extension"]["uka"]["source_record"]["catalogue"]["id"]
    state = load_state(data)
    assert {i["status"] for i in state["indicators"].values()} == {"published"}
    assert "withheld" not in state
    envelope = store(data).get("uka/72/sv")
    assert set(envelope["responses"]) == {"catalogue", "totals", "links"}
    assert envelope["responses"]["catalogue"]["row"]["id"] == 72
    assert set(envelope["derived"]) == {"scan", "skipped_periods"}
    assert envelope["derived"]["scan"]["files"]
    assert not list((data / "source").rglob("*.csv"))
    assert store(data).get_shared("uka", "indicators.json")
    assert not (data / "metadata").exists()


def test_unchanged_rerun_downloads_nothing_and_is_byte_identical(data, tmp_path):
    before = stable(data)
    manifest = (data / MANIFEST).read_bytes()
    source = Source()
    result = execute(source, data, tmp_path / "rerun")
    assert result["counts"]["reused"] == 2
    assert source.downloads == []
    assert len(source.heads) == 2
    assert (data / MANIFEST).read_bytes() == manifest
    assert stable(data) == before


def test_changed_link_rebuilds_only_that_indicator(data, tmp_path):
    source = Source()
    new = source.replace_newest(72)
    result = execute(source, data, tmp_path / "changed")
    assert result["counts"] == {**result["counts"], "rebuilt": 1, "reused": 1}
    assert new in source.downloads
    assert all("andel-disputerade" not in url for url in source.downloads)  # 67
    assert new in store(data).get("uka/72/sv")["responses"]["links"].values()
    files = load_documents(data)["72"]["extension"]["uka"]["files"]
    assert new in {file["url"] for file in files.values()}


def test_mapping_version_and_full_rebuild_everything(data, tmp_path, monkeypatch):
    result = execute(Source(), data, tmp_path / "full", full=True)
    assert result["counts"]["rebuilt"] == 2
    monkeypatch.setattr(
        "apps.harvest.metadata_adapters.uka.workflow.MAPPING_VERSION", 99
    )
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
    assert state["indicators"]["72"]["status"] == "withheld"
    assert state["indicators"]["72"]["error"]["stage"] == "endpoint"
    assert load_documents(data, include_withheld=True)["72"] == before

    source = Source()
    result = execute(source, data, tmp_path / "recovered")
    assert result["status"] == "success"
    assert result["counts"]["restored"] == 1
    assert source.downloads == []
    assert load_documents(data)["72"] == before


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


def test_unreadable_export_keeps_stored_scan_until_it_is_stale(data, tmp_path):
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
    assert "uka/72/sv" not in store(data).entries()


def test_catalogue_failure_publishes_nothing(data, tmp_path):
    from apps.harvest.metadata_adapters.uka.workflow import run

    source = Source()
    source.catalogue_error = Unreachable("UKÄ is down")
    before = harvest_manifest(data)
    with pytest.raises(Unreachable):
        asyncio.run(run(PROVIDER, data, tmp_path / "out", client=source, clock=NOW))
    assert harvest_manifest(data) == before


def test_bounded_run_touches_only_selected(data, tmp_path):
    source = Source(ids=(67,))  # 72 missing, but a bounded run proves nothing
    later = NOW + timedelta(days=1)
    execute(source, data, tmp_path / "bounded", clock=later, indicators=["67"])
    state = load_state(data)
    assert state["indicators"]["67"]["checked_at"] == later.isoformat()
    assert state["indicators"]["72"]["checked_at"] == NOW.isoformat()
    assert set(load_documents(data)) == {"67", "72"}


def test_stale_artifact_rejected(data, tmp_path):
    from apps.harvest.metadata_adapters.uka.workflow import collect, run

    asyncio.run(run(PROVIDER, data, tmp_path / "out", client=Source(), clock=NOW))
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out")

"""Weekly Trafikanalys lifecycle against recorded responses."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from apps.harvest.metadata_adapters.trafa.client import Unreachable
from apps.harvest.metadata_adapters.trafa.state import load_state
from apps.harvest.source_store import SourceStore, harvest_manifest, source_key
from tests.support.binding import binding
from tests.support.parsing import documents
from tests.support.trafa import RecordedClient, fixture_key

NOW = datetime(2026, 10, 12, 6, tzinfo=UTC)
PROVIDER = binding("trafa", "trafa", languages=["sv"], retry_cooldown_seconds=0)
PRODUCTS = "https://api.trafa.se/api/structure"
DOCS = "https://www.trafa.se/sidor/api-dokumentation/"
MANIFEST = "source/manifest.json"


class Source(RecordedClient):
    """Recorded answers; failing or unrecorded requests are unreachable, like
    ``TrafaClient`` after its retries."""

    def _answer(self, url, params):
        try:
            return super()._answer(url, params)
        except (TimeoutError, LookupError) as error:
            raise Unreachable(str(error)) from error


def start(source, data, output, *, clock=NOW, **options):
    from apps.harvest.metadata_adapters.trafa.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from apps.harvest.metadata_adapters.trafa.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data):
    return documents("trafa", data, [PROVIDER])


def store(data):
    return SourceStore(data / "source", "trafa")


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data" / "trafa"
    result = execute(Source(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(published(root)) > 0
    assert set(result["excluded"]) == {"ATFT"}
    return root


def test_bootstrap_stores_raw_probes_and_shared_structures(data):
    from apps.harvest.validation import validate_metadata

    entries = store(data).entries()
    assert {key.split("/")[1].split("|")[0] for key in entries} == {
        "T10011",
        "T10033",
        "T04010",
    }
    key = next(iter(entries))
    envelope = store(data).get(key)
    assert set(envelope["responses"]) == {"probe"}
    assert "Header" in envelope["responses"]["probe"]
    assert envelope["updated"] is None
    shared = store(data).manifest["shared"]
    assert "trafa/_shared/products.json" in shared
    assert "trafa/_shared/structure.T10011.sv.json" in shared
    assert "trafa/_shared/documentation.T10011.html" in shared
    assert not (data / "metadata").exists()
    for document in published(data).values():
        validate_metadata(document, adapter="trafa")
        assert document["extension"]["trafa"]["source_record"]["product"]["unique_id"]


def test_rerun_is_byte_identical_and_unchanged(data, tmp_path):
    before = harvest_manifest(data)
    manifest = (data / MANIFEST).read_bytes()
    result = execute(Source(), data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == len(published(data))
    assert (data / MANIFEST).read_bytes() == manifest
    assert {
        k: v for k, v in harvest_manifest(data).items() if k != "reports/latest.json"
    } == {k: v for k, v in before.items() if k != "reports/latest.json"}


def test_failing_probe_is_withheld_then_restored(data, tmp_path):
    source = Source()
    probes = [k for k in source.responses if k.startswith(f"{PRODUCTS[:-9]}data?")]
    source.failing = {k for k in probes if "t10033" in k}
    before = {k: d for k, d in published(data).items() if "T10033" in k}
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["withheld"] == len(before)
    assert not any("T10033" in key for key in published(data))
    state = load_state(data)
    assert "withheld" not in state
    for key in before:
        assert state["datasets"][key]["status"] == "withheld"
        language, code = key.split("/", 1)
        assert store(data).get(source_key("trafa", code, language)) is not None
    assert {
        k: d
        for k, d in documents("trafa", data, [PROVIDER], include_withheld=True).items()
        if "T10033" in k
    } == before

    result = execute(Source(), data, tmp_path / "back")
    assert result["counts"]["restored"] == len(before)
    assert {k: d for k, d in published(data).items() if "T10033" in k} == before


def test_empty_body_keeps_previous_envelope(data, tmp_path):
    source = Source()
    probes = [k for k in source.responses if "data?" in k and "t10033" in k]
    for key in probes:
        source.responses[key] = {}
    before = {k: d for k, d in published(data).items() if "T10033" in k}
    envelopes = {
        key: store(data).get(key) for key in store(data).entries() if "T10033" in key
    }
    result = execute(source, data, tmp_path / "empty")
    assert result["status"] == "completed_with_errors"
    assert {e["stage"] for e in result["errors"]} == {"metadata"}
    assert all("T10033" in e["resource"] for e in result["errors"])
    assert {key: store(data).get(key) for key in envelopes} == envelopes
    assert {k: d for k, d in published(data).items() if "T10033" in k} == before


def test_failing_documentation_page_keeps_documents(data, tmp_path):
    before = published(data)
    source = Source()
    source.failing = {fixture_key(DOCS, {"code": "T10011"})}
    result = execute(source, data, tmp_path / "docs-down")
    assert [e["resource"] for e in result["errors"]] == ["*/T10011"]
    assert result["errors"][0]["stage"] == "metadata"
    assert result["counts"]["removed"] == 0
    assert published(data) == before

    later = NOW + timedelta(days=61)
    execute(source, data, tmp_path / "stale", clock=later)
    assert not any("T10011" in key for key in published(data))


def test_product_removed_from_list_is_removed(data, tmp_path):
    source = Source()
    listing = source.responses[fixture_key(PRODUCTS, {"lang": "SV"})]
    listing["StructureItems"] = [
        item for item in listing["StructureItems"] if item.get("UniqueId") != "T04010"
    ]
    result = execute(source, data, tmp_path / "removed")
    assert result["counts"]["removed"] > 0
    assert not any("T04010" in key for key in published(data))
    assert not any("T04010" in key for key in store(data).entries())
    assert not list((data / "source/trafa").glob("T04010*"))
    assert store(data).get_shared("trafa", "structure.T04010.sv.json") is None
    assert store(data).get_shared("trafa", "documentation.T04010.html") is None


def test_product_list_failure_publishes_nothing(data, tmp_path):
    source = Source()
    source.failing = {fixture_key(PRODUCTS, {"lang": "SV"})}
    before = harvest_manifest(data)
    with pytest.raises(Unreachable):
        start(source, data, tmp_path / "out")
    assert harvest_manifest(data) == before


def test_restricted_product_run_keeps_other_products_files(data, tmp_path):
    later = NOW + timedelta(days=1)
    before = harvest_manifest(data)
    execute(Source(), data, tmp_path / "bounded", clock=later, products=["T10033"])
    after = harvest_manifest(data)
    assert {k for k in before if k.startswith("source/") and "T10033" not in k} <= set(
        after
    )
    assert store(data).get_shared("trafa", "structure.T10011.sv.json") is not None
    state = load_state(data)["datasets"]
    assert {
        info["checked_at"] == later.isoformat()
        for key, info in state.items()
        if "T10033" in key
    } == {True}
    assert {
        info["checked_at"] for key, info in state.items() if "T10033" not in key
    } == {NOW.isoformat()}


def test_stale_artifact_rejected(data, tmp_path):
    from apps.harvest.metadata_adapters.trafa.workflow import collect

    start(Source(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])

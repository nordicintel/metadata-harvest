"""Weekly Trafikanalys lifecycle against recorded responses."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harvesting.adapters.trafa.client import Unreachable
from harvesting.adapters.trafa.state import collection, load_documents, load_state
from tests.trafa.conftest import RecordedClient, fixture_key

NOW = datetime(2026, 10, 12, 6, tzinfo=UTC)
PROVIDER = {"code": "trafa", "languages": ["sv"], "retry_cooldown_seconds": 0}
PRODUCTS = "https://api.trafa.se/api/structure"
DOCS = "https://www.trafa.se/sidor/api-dokumentation/"


class Source(RecordedClient):
    """Recorded answers; failing or unrecorded requests are unreachable, like
    ``TrafaClient`` after its retries."""

    def _answer(self, url, params):
        try:
            return super()._answer(url, params)
        except (TimeoutError, LookupError) as error:
            raise Unreachable(str(error)) from error


def start(source, data, output, *, clock=NOW, **options):
    from harvesting.adapters.trafa.workflow import run

    return asyncio.run(
        run(PROVIDER, data, output, client=source, clock=clock, **options)
    )


def execute(source, data, output, **options):
    from harvesting.adapters.trafa.workflow import collect

    result = start(source, data, output, **options)
    collect(data, output, languages=["sv"])
    return result


def published(data):
    return load_documents(data, ["sv"])


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    result = execute(Source(), root, tmp_path / "bootstrap")
    assert result["status"] == "success", result["errors"]
    assert result["counts"]["rebuilt"] == len(published(root)) > 0
    assert set(result["excluded"]) == {"ATFT"}
    return root


def test_bootstrap_publishes_valid_datasets_of_recorded_products(data):
    from harvesting.validation import validate_metadata

    documents = published(data)
    assert {key.split("/", 1)[1].split("|")[0] for key in documents} == {
        "T10011",
        "T10033",
        "T04010",
    }
    for document in documents.values():
        validate_metadata(document, adapter="trafa")
        assert document["extension"]["trafa"]["source_record"]["product"]["unique_id"]


def test_rerun_is_byte_identical_and_unchanged(data, tmp_path):
    before = (data / collection("sv")).read_bytes()
    result = execute(Source(), data, tmp_path / "rerun")
    assert result["counts"]["rebuilt"] == 0
    assert result["counts"]["unchanged"] == len(published(data))
    assert (data / collection("sv")).read_bytes() == before


def test_failing_probe_is_withheld_then_restored(data, tmp_path):
    source = Source()
    probes = [k for k in source.responses if k.startswith(f"{PRODUCTS[:-9]}data?")]
    source.failing = {k for k in probes if "t10033" in k}
    before = {k: d for k, d in published(data).items() if "T10033" in k}
    result = execute(source, data, tmp_path / "down")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["withheld"] == len(before)
    assert not any("T10033" in key for key in published(data))
    assert {k: load_state(data)["withheld"][k] for k in before} == before

    result = execute(Source(), data, tmp_path / "back")
    assert result["counts"]["restored"] == len(before)
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


def test_product_list_failure_publishes_nothing(data, tmp_path):
    source = Source()
    source.failing = {fixture_key(PRODUCTS, {"lang": "SV"})}
    before = (data / collection("sv")).read_bytes()
    with pytest.raises(Unreachable):
        start(source, data, tmp_path / "out")
    assert (data / collection("sv")).read_bytes() == before


def test_bounded_run_touches_only_selected(data, tmp_path):
    later = NOW + timedelta(days=1)
    execute(Source(), data, tmp_path / "bounded", clock=later, products=["T10033"])
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
    from harvesting.adapters.trafa.workflow import collect

    start(Source(), data, tmp_path / "out")
    (data / "reports/latest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Stale"):
        collect(data, tmp_path / "out", languages=["sv"])

"""Parsing Trafikanalys source files reproduces the accepted collection."""

from pathlib import Path

from apps.harvest.source_store import SourceStore
from tests.harvest.trafa.test_workflow import PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/trafa/accepted")


def test_parse_reproduces_previous_collection_bytes(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "trafa"
    execute(Source(), root, tmp_path / "bootstrap")
    result = build("trafa", tmp_path / "data", tmp_path / "out", providers=[PROVIDER])
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    produced = tmp_path / "out/metadata" / golden.name
    assert produced.read_bytes() == golden.read_bytes()


def test_parse_reports_probe_errors_for_one_dataset_and_keeps_siblings(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "trafa"
    execute(Source(), root, tmp_path / "bootstrap")
    store = SourceStore(root / "source", "trafa")
    key = next(k for k in store.entries() if "T10033" in k)
    store.put(
        key,
        responses={"probe": {"Errors": ["Produkten hittades inte"]}},
        updated=None,
        retrieved_at="2026-10-16T00:00:00+00:00",
    )
    store.save()
    result = build("trafa", tmp_path / "data", tmp_path / "out", providers=[PROVIDER])
    assert result["status"] == "completed_with_errors"
    assert [e["key"] for e in result["errors"]] == [key]
    assert result["counts"]["parsed"] == len(store.entries()) - 1

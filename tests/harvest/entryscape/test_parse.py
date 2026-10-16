"""Parsing EntryScape source files reproduces the accepted collections."""

from pathlib import Path

from tests.support.entryscape import (
    PROVIDERS,
    execute,
    source,  # noqa: F401  (fixture)
)

GOLDEN = Path("tests/fixtures/entryscape/accepted")


def test_parse_reproduces_previous_collection_bytes(source, tmp_path):  # noqa: F811
    from apps.parsing.build import build

    root = tmp_path / "data" / "entryscape"
    execute(source, root, tmp_path / "bootstrap")
    result = build(
        "entryscape", tmp_path / "data", tmp_path / "out", bindings=PROVIDERS
    )
    assert result["status"] == "success", result["errors"]
    for golden in sorted(GOLDEN.glob("*.jsonl.gz")):
        assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
            golden.read_bytes()
        ), golden.name


def test_regrouping_needs_no_requests(source, tmp_path, monkeypatch):  # noqa: F811
    from apps.harvest.metadata_adapters.entryscape import datasets
    from apps.parsing.build import build

    root = tmp_path / "data" / "entryscape"
    execute(source, root, tmp_path / "bootstrap")
    monkeypatch.setattr(datasets, "MAPPING_VERSION", 99)
    source.calls.clear()
    result = build(
        "entryscape", tmp_path / "data", tmp_path / "out", bindings=PROVIDERS
    )
    assert result["status"] == "success"
    assert source.calls == []

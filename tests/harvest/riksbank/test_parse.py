"""Parsing Riksbank source files reproduces the accepted collection."""

from pathlib import Path

from tests.support.riksbank import PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/riksbank/accepted")


def test_parse_reproduces_previous_collection_bytes(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "riksbank"
    execute(Source(), root, tmp_path / "bootstrap")
    result = build("riksbank", tmp_path / "data", tmp_path / "out", bindings=[PROVIDER])
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )

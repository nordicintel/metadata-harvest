"""Parsing SMHI emissions source files reproduces the accepted collection."""

from pathlib import Path

from tests.harvest.smhi_emissions.test_workflow import PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/smhi_emissions/accepted")


def test_parse_reproduces_previous_collection_bytes(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "smhi_emissions"
    execute(Source(), root, tmp_path / "bootstrap")
    result = build(
        "smhi_emissions", tmp_path / "data", tmp_path / "out", providers=[PROVIDER]
    )
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )

"""Parsing PXWeb v2 source files reproduces the accepted collection."""

from pathlib import Path

from tests.harvest.test_pxweb_v2 import PROVIDER
from tests.harvest.test_pxweb_v2_workflow import Source, harvest

GOLDEN = Path("tests/fixtures/pxweb_v2/accepted")


def test_parse_reproduces_previous_collection_bytes(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "pxweb_v2"
    harvest(Source(), root, tmp_path / "bootstrap", full=True)
    result = build(
        "pxweb_v2", tmp_path / "data", tmp_path / "out", providers=[PROVIDER]
    )
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )

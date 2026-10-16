"""Parsing UKÄ source files reproduces the accepted collection."""

from pathlib import Path

from tests.support.uka import PROVIDER, Source, execute

GOLDEN = Path("tests/fixtures/uka/accepted")


def test_parse_reproduces_previous_collection_bytes(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "uka"
    execute(Source(), root, tmp_path / "bootstrap")
    result = build("uka", tmp_path / "data", tmp_path / "out", bindings=[PROVIDER])
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )


def test_scan_summary_round_trips():
    from apps.harvest.metadata_adapters.uka.mapping import Scan

    source = Source()
    scan = Scan()
    for (indicator, period), url in source.links.items():
        if indicator == 67 and url in source.files:
            text, modified = source.files[url]
            scan.add(period, text, url, modified)
    assert scan.files
    summary = scan.to_summary()
    restored = Scan.from_summary(summary)
    assert restored == scan
    assert restored.to_summary() == summary

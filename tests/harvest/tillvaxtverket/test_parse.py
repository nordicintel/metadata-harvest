"""Parsing Tillväxtverket source files reproduces the accepted collection."""

from pathlib import Path

from tests.support.tillvaxtverket import (
    NAMES,
    PROVIDER,
    Source,
    execute,
    snapshot_from,
)

GOLDEN = Path("tests/fixtures/tillvaxtverket/accepted")


def test_parse_reproduces_previous_collection_bytes_with_portal_snapshot(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "tillvaxtverket"
    execute(Source(), root, tmp_path / "bootstrap")
    portal = snapshot_from(tmp_path / "data" / "data_eu", NAMES)
    result = build(
        "tillvaxtverket",
        tmp_path / "data",
        tmp_path / "out",
        bindings=[PROVIDER],
        portal=portal,
    )
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )


def test_parse_without_a_snapshot_record_falls_back_to_the_dataset_name(tmp_path):
    from apps.parsing.build import build

    root = tmp_path / "data" / "tillvaxtverket"
    execute(Source(), root, tmp_path / "bootstrap")
    portal = snapshot_from(tmp_path / "data" / "data_eu", NAMES[1:])
    result = build(
        "tillvaxtverket",
        tmp_path / "data",
        tmp_path / "out",
        bindings=[PROVIDER],
        portal=portal,
    )
    assert result["status"] == "success", result["errors"]
    from tests.support.parsing import documents

    parsed = documents("tillvaxtverket", root, [PROVIDER], portal=portal)
    first = parsed[f"sv/inkvartering/{NAMES[0]}"]
    assert first["label"] == NAMES[0]
    assert "description" not in first["extension"]["nordicintel"]
    assert parsed[f"sv/inkvartering/{NAMES[1]}"]["label"] != NAMES[1]

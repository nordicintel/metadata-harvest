"""Parsing Kolada source files reproduces the accepted collection."""

from pathlib import Path

from tests.support.kolada import (
    PROVIDER,
    initial_state,
    seed_source,  # noqa: F401  (fixture)
)

GOLDEN = Path("tests/fixtures/kolada/accepted")


def test_parse_reproduces_previous_collection_bytes(seed_source, tmp_path):  # noqa: F811
    from apps.parsing.build import build

    root = tmp_path / "data" / "kolada"
    initial_state(seed_source, root)
    result = build(
        "kolada",
        tmp_path / "data",
        tmp_path / "out",
        bindings=[PROVIDER],
    )
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )

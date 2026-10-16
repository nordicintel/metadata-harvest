"""Parsing SDB source files reproduces the accepted collection."""

from pathlib import Path

from apps.harvest.metadata_adapters.sdb.state import PROVIDER_CODE
from apps.harvest.source_store import source_key
from tests.harvest.sdb.test_workflow import (
    PROVIDER,
    execute,
    first_codes_row,
    seeded,  # noqa: F401  (fixture)
)

GOLDEN = Path("tests/fixtures/sdb/accepted")


def test_parse_reproduces_previous_collection_bytes(seeded, tmp_path):  # noqa: F811
    from apps.parsing.build import build

    root = tmp_path / "data" / "sdb"
    execute(seeded, root, tmp_path / "bootstrap")
    result = build("sdb", tmp_path / "data", tmp_path / "out", bindings=[PROVIDER])
    assert result["status"] == "success", result["errors"]
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "out/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )


def test_unreconcilable_result_fields_fail_at_parse_and_keep_siblings(
    seeded,  # noqa: F811
    tmp_path,
):
    from apps.parsing.build import build

    seeded.fake.result_names["amning"] = {"variabel": "grupp", "alder": "age"}
    row = first_codes_row("amning")
    row["gruppId"] = row.pop("variabelId")
    row["ageId"] = row.pop("alderId")
    seeded.fake.rows["amning"] = [row]
    root = tmp_path / "data" / "sdb"
    result = execute(seeded, root, tmp_path / "bootstrap")
    assert result["status"] == "success"  # the harvest stores what SDB returned
    parsed = build("sdb", tmp_path / "data", tmp_path / "out", bindings=[PROVIDER])
    assert parsed["status"] == "completed_with_errors"
    assert [e["key"] for e in parsed["errors"]] == [
        source_key(PROVIDER_CODE, "amning", "sv")
    ]
    assert "Cannot match SDB variables" in parsed["errors"][0]["error"]
    assert parsed["counts"]["parsed"] == 3

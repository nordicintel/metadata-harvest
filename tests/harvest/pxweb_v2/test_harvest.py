"""PXWeb v2: the catalogue-driven loop against controlled HTTP responses."""

from pathlib import Path

import pytest

from apps.harvest.metadata_adapters.pxweb_v2 import parse
from apps.harvest.metadata_adapters.pxweb_v2.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from tests.support.pxweb_v2 import PROVIDER, Source, harvest, table

GOLDEN = Path("tests/fixtures/pxweb_v2/accepted")


def metadata_bytes(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in (root / "source").rglob("*.json")
    }


def store(root):
    return SourceStore(root / "source", "pxweb_v2")


def parsed(root):
    return dict(parse.documents(store(root), {}, [PROVIDER]))


def test_full_run_stores_tables_and_parses_to_the_accepted_collection(tmp_path):
    from apps.parsing.build import build

    source, root = Source(), tmp_path / "data" / "pxweb_v2"
    initial = harvest(source, root, tmp_path / "first", full=True)
    assert initial["status"] == "success" and initial["counts"]["created"] == 2
    assert set(store(root).entries()) == {"scb/T1/sv", "scb/EN1/en"}
    assert set(store(root).get("scb/T1/sv")["responses"]) == {"metadata", "table"}
    assert store(root).get("scb/T1/sv")["updated"] == table()["updated"]
    assert len(list((root / "codelists").glob("*.jsonl.gz"))) == 2
    assert sorted(p.name for p in (root / "reports").iterdir()) == [
        "harvest.json",
        "refresh.json",
    ]
    documents = parsed(root)
    assert not any(isinstance(d, Exception) for d in documents.values())
    built = build(
        "pxweb_v2", tmp_path / "data", tmp_path / "parsed", bindings=[PROVIDER]
    )
    golden = next(GOLDEN.glob("*.jsonl.gz"))
    assert (tmp_path / "parsed/metadata" / golden.name).read_bytes() == (
        golden.read_bytes()
    )
    assert built["counts"]["parsed"] == 2


def test_refresh_reuses_unchanged_tables_and_a_full_run_refetches(tmp_path):
    source, root = Source(), tmp_path / "data"
    harvest(source, root, tmp_path / "first", full=True)
    before = metadata_bytes(root)
    source.requests.clear()
    unchanged = harvest(source, root, tmp_path / "second")
    assert unchanged["counts"]["reused"] == 2
    assert not any(
        path.endswith("/metadata") or "/codelists/" in path
        for path, _ in source.requests
    )
    assert metadata_bytes(root) == before
    source.label = "Corrected upstream without timestamp"
    full = harvest(source, root, tmp_path / "third", full=True)
    assert full["counts"]["updated"] == 2
    assert {d["label"] for d in parsed(root).values()} == {source.label}


def test_removal_is_language_isolated_and_stale_artifacts_are_rejected(tmp_path):
    source, root = Source(), tmp_path / "data"
    first = harvest(source, root, tmp_path / "first")
    source.tables["sv"] = []
    source.fail_languages.add("en")
    result = harvest(source, root, tmp_path / "second")
    assert result["status"] == "completed_with_errors"
    assert result["counts"]["removed"] == 1
    assert list(store(root).entries()) == ["scb/EN1/en"]
    assert ADAPTER.scope_failures(result) == ["en"]
    with pytest.raises(ValueError, match="Stale"):
        lifecycle.collect(ADAPTER, root, tmp_path / "first")
    assert first["run_id"] != result["run_id"]
    source.fail_languages.clear()
    source.requests.clear()
    budget = harvest(source, root, tmp_path / "third", full=True, budget_seconds=-1)
    assert (
        budget["counts"]["failed"] == 1 and budget["status"] == "completed_with_errors"
    )
    assert not any(path.endswith("/metadata") for path, _ in source.requests)

"""Offline parsing turns per-dataset source files into accepted collections."""

import gzip
import json
import sys
import types
from pathlib import Path

import pytest

from apps.harvest.source_store import SourceStore, source_key
from tests.support.worker_document import legacy

ADAPTER = "pxweb_v2"
PROVIDERS = [{"code": "scb"}]
STAMP = "2026-10-16T05:00:00+00:00"


def fake_parse(monkeypatch, *, failing=(), withheld=()):
    """Register a parse module whose documents come from the stored envelope."""

    def documents(store, state, providers, *, portal=None):
        assert providers == PROVIDERS
        for key in store.entries():
            envelope = store.get(key)
            if key in failing:
                yield key, ValueError("broken mapping")
            else:
                yield key, envelope["responses"]["document"]

    module = types.ModuleType(f"apps.harvest.metadata_adapters.{ADAPTER}.parse")
    module.documents = documents
    module.withheld_keys = lambda state: set(withheld)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def document(code: str) -> dict:
    """A worker document that passes strict validation (no custom category keys)."""
    value = legacy("sv", code)
    value["dimension"]["Region"]["category"].pop("custom")
    return value


def seed(data: Path, *codes: str) -> list[str]:
    store = SourceStore(data / ADAPTER / "source", ADAPTER)
    keys = []
    for code in codes:
        key = source_key("scb", code, "sv")
        store.put(
            key,
            responses={"document": document(code)},
            updated="2026-01-01",
            retrieved_at=STAMP,
        )
        keys.append(key)
    store.save()
    return keys


def collection(root: Path) -> list[dict]:
    paths = sorted((root / "metadata").glob("*.jsonl.gz"))
    assert len(paths) == 1
    with gzip.open(paths[0], "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def test_build_writes_collection_and_report(tmp_path, monkeypatch):
    from apps.parsing.build import build

    fake_parse(monkeypatch)
    seed(tmp_path / "data", "T1", "T2")
    result = build(ADAPTER, tmp_path / "data", tmp_path / "out", providers=PROVIDERS)
    assert result["status"] == "success"
    assert result["counts"] == {"parsed": 2, "failed": 0, "withheld": 0}
    assert {
        d["extension"]["nordicintel"]["dataset_code"]
        for d in collection(tmp_path / "out")
    } == {
        "T1",
        "T2",
    }
    report = json.loads((tmp_path / "out/reports/parse.json").read_text("utf-8"))
    assert report["adapter"] == ADAPTER and report["errors"] == []
    artifact = json.loads((tmp_path / "out/result.json").read_text("utf-8"))
    assert artifact["base_manifest"] == {}
    assert set(artifact["manifest"]) >= {
        "reports/parse.json",
        *(f"metadata/{name}" for name in report["collections"]),
    }


def test_build_reports_failed_documents_and_writes_siblings(tmp_path, monkeypatch):
    from apps.parsing.build import build

    fake_parse(monkeypatch, failing={"scb/T2/sv"})
    seed(tmp_path / "data", "T1", "T2")
    result = build(ADAPTER, tmp_path / "data", tmp_path / "out", providers=PROVIDERS)
    assert result["status"] == "completed_with_errors"
    assert result["counts"] == {"parsed": 1, "failed": 1, "withheld": 0}
    assert result["errors"] == [
        {"key": "scb/T2/sv", "error": "ValueError: broken mapping"}
    ]
    assert [
        d["extension"]["nordicintel"]["dataset_code"]
        for d in collection(tmp_path / "out")
    ] == ["T1"]


def test_build_skips_withheld_entries_and_rejects_invalid_documents(
    tmp_path, monkeypatch
):
    from apps.parsing.build import build

    fake_parse(monkeypatch, withheld={"scb/T2/sv"})
    seed(tmp_path / "data", "T1", "T2", "T3")
    store = SourceStore(tmp_path / "data" / ADAPTER / "source", ADAPTER)
    broken = document("T3")
    broken["value"] = [1]  # observations are never accepted metadata
    store.put(
        "scb/T3/sv", responses={"document": broken}, updated=None, retrieved_at=STAMP
    )
    store.save()
    result = build(ADAPTER, tmp_path / "data", tmp_path / "out", providers=PROVIDERS)
    assert result["counts"] == {"parsed": 1, "failed": 1, "withheld": 1}
    assert result["errors"][0]["key"] == "scb/T3/sv"
    assert [
        d["extension"]["nordicintel"]["dataset_code"]
        for d in collection(tmp_path / "out")
    ] == ["T1"]


def test_apply_refuses_changed_baseline_then_replaces(tmp_path, monkeypatch):
    from apps.parsing.build import apply, build

    fake_parse(monkeypatch)
    data, out = tmp_path / "data", tmp_path / "out"
    seed(data, "T1")
    metadata = data / ADAPTER / "metadata"
    metadata.mkdir(parents=True)
    stale = metadata / "old__multilingual__000000000000.jsonl.gz"
    stale.write_bytes(gzip.compress(b""))
    build(ADAPTER, data, out, providers=PROVIDERS)
    (metadata / "intruder.jsonl.gz").write_bytes(b"x")
    with pytest.raises(ValueError, match="changed since"):
        apply(ADAPTER, data, out)
    (metadata / "intruder.jsonl.gz").unlink()
    applied = apply(ADAPTER, data, out)
    assert sorted(p.name for p in metadata.iterdir()) == applied["collections"]
    assert not stale.exists()
    assert (
        collection(data / ADAPTER)[0]["extension"]["nordicintel"]["dataset_code"]
        == "T1"
    )
    assert (data / ADAPTER / "reports/parse.json").exists()


def test_apply_refuses_empty_or_shrinking_source(tmp_path, monkeypatch):
    from apps.parsing.build import apply, build

    fake_parse(monkeypatch)
    data, out = tmp_path / "data", tmp_path / "out"
    seed(data, "T1", "T2")
    build(ADAPTER, data, out, providers=PROVIDERS)
    apply(ADAPTER, data, out)
    assert len(collection(data / ADAPTER)) == 2

    store = SourceStore(data / ADAPTER / "source", ADAPTER)
    store.remove("scb/T2/sv")
    store.save()
    result = build(ADAPTER, data, out, providers=PROVIDERS)
    assert result["committed_datasets"] == 2 and result["counts"]["parsed"] == 1
    with pytest.raises(ValueError, match="allow-shrink"):
        apply(ADAPTER, data, out)
    assert len(collection(data / ADAPTER)) == 2  # untouched
    apply(ADAPTER, data, out, allow_shrink=True)
    assert len(collection(data / ADAPTER)) == 1

    store.remove("scb/T1/sv")
    store.save()
    build(ADAPTER, data, out, providers=PROVIDERS)
    with pytest.raises(ValueError, match="no source entries"):
        apply(ADAPTER, data, out, allow_shrink=True)
    assert len(collection(data / ADAPTER)) == 1


def test_cli_build_all_iterates_available_adapters(tmp_path, monkeypatch):
    from apps.operations import parsing as cli

    calls = []
    monkeypatch.setattr(
        cli,
        "build",
        lambda adapter, data, output, **kw: (
            calls.append((adapter, output)) or {"status": "success"}
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["parsing", "build", "--all", "--output", str(tmp_path)]
    )
    cli.main()
    from apps.harvest.registry import ADAPTERS

    assert [c[0] for c in calls] == [k for k, v in ADAPTERS.items() if v.available]
    assert calls[0][1] == tmp_path / calls[0][0]

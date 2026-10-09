"""Offline catalog boundaries and failure isolation."""

import gzip
import json
from pathlib import Path

import pytest

from metadata_harvest.catalog import build, promote


def collection(root, content=b'{"trusted":true}\n'):
    path = root / "pxweb_v1/metadata/test__sv.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))
    return path


def test_offline_trusted_copy_reuse_and_removal(tmp_path, monkeypatch):
    import socket

    import metadata_harvest.validation as validation

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden")
    )
    monkeypatch.setattr(
        validation,
        "validate_metadata",
        lambda *a, **k: pytest.fail("trusted corpus validation forbidden"),
    )
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    first = build(data, candidate, adapters=["pxweb_v1"])
    assert first["counts"]["copied"] == 1
    assert first["stages"] == {
        "content_enrichment": "not_configured",
        "semantic_enrichment": "not_configured",
    }
    promote(data, candidate)
    target = data / "catalog/pxweb_v1/metadata/test__sv.jsonl.gz"
    assert target.read_bytes() == source.read_bytes()
    second = build(data, candidate, adapters=["pxweb_v1"])
    assert second["counts"]["reused"] == 1
    promote(data, candidate)
    source.write_bytes(gzip.compress(b"", mtime=0))
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    assert gzip.decompress(target.read_bytes()) == b""


def test_corrupt_and_missing_retain_previous_other_collections_advance(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    target = data / "catalog/pxweb_v1/metadata/test__sv.jsonl.gz"
    before = target.read_bytes()
    source.write_bytes(b"broken gzip")
    other = source.with_name("other.jsonl.gz")
    other.write_bytes(gzip.compress(b"{}\n", mtime=0))
    result = build(data, candidate, adapters=["pxweb_v1"])
    assert result["status"] == "completed_with_errors"
    promote(data, candidate)
    assert target.read_bytes() == before
    assert (target.parent / other.name).read_bytes() == other.read_bytes()
    source.unlink()
    result = build(data, candidate, adapters=["pxweb_v1"])
    assert any("Missing" in error["error"] for error in result["errors"])


def test_stale_and_modified_candidates_rejected(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    source.write_bytes(gzip.compress(b"{}\n", mtime=0))
    with pytest.raises(ValueError, match="Stale"):
        promote(data, candidate)
    build(data, candidate, adapters=["pxweb_v1"])
    (candidate / "pxweb_v1/metadata/test__sv.jsonl.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="Candidate"):
        promote(data, candidate)


def test_upstream_errors_remain_visible(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    report = data / "pxweb_v1/reports/harvest.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps(
            {"status": "completed_with_errors", "errors": [{"resource": "bad table"}]}
        )
    )
    result = build(data, candidate, adapters=["pxweb_v1"])
    assert (
        result["upstream"]["pxweb_v1"]["harvest.json"]["errors"][0]["resource"]
        == "bad table"
    )
    assert result["status"] == "completed_with_errors"


def test_catalog_import_does_not_load_harvesting():
    import subprocess
    import sys

    code = "import metadata_harvest.catalog,sys; assert not any(x in sys.modules for x in ['metadata_harvest.runner','metadata_harvest.workflow','httpx','playwright'])"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_catalog_publication_rejects_unrelated_staging(tmp_path, monkeypatch):
    import subprocess

    from metadata_harvest.publication import publish

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    collection(tmp_path / "data/catalog")
    (tmp_path / "unrelated.txt").write_text("unrelated")
    git("add", "unrelated.txt")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="unrelated staged"):
        publish("catalog", data=tmp_path / "data/catalog")
    assert git("diff", "--cached", "--name-only").stdout.strip() == "unrelated.txt"


def test_cli_rejects_uncommitted_source_results(tmp_path, monkeypatch):
    import subprocess

    from metadata_harvest.catalog import verify_published

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    collection(tmp_path / "data")
    with pytest.raises(ValueError, match="committed"):
        verify_published(Path("data"), ["pxweb_v1"])


def test_workflow_dispatch_is_one_way_and_before_error_exit():
    root = Path(__file__).resolve().parents[1]
    catalog = (root / ".github/workflows/catalog.yml").read_text()
    assert "metadata_harvest.runner" not in catalog
    assert "gh workflow run" not in catalog
    assert "schedule:" not in catalog
    for name in ("harvest.yml", "harvest-pxweb-v2.yml", "harvest-kolada.yml"):
        content = (root / ".github/workflows" / name).read_text()
        assert content.index("gh workflow run catalog.yml") > content.rindex(
            "metadata_harvest.runner publish"
        )
        assert content.index("gh workflow run catalog.yml") < content.index(
            "      - name: Report incomplete"
        )


def test_cli_rejects_ignored_source_files(tmp_path, monkeypatch):
    import subprocess

    from metadata_harvest.catalog import verify_published

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True
        )

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text("output/\n")
    git("add", ".gitignore")
    git("commit", "-m", "initial")
    collection(tmp_path / "output/worker")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="committed"):
        verify_published(Path("output/worker"), ["pxweb_v1"])

"""Offline catalog boundaries and failure isolation."""

import gzip
import json
from pathlib import Path

import pytest

from apps.paths import ROOT


def unpack(path):
    from apps.publication.archives import open_catalog

    with open_catalog(path) as stream:
        return stream.read()


def collection(root, content=b'{"trusted":true}\n'):
    path = root / "pxweb_v1/metadata/test__sv.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))
    return path


def test_streaming_lines_without_extracted_files(tmp_path):
    import io

    from apps.publication.archives import _compress, open_catalog

    source = tmp_path / "catalog.jsonl"
    source.write_text('{"title":"åäö"}\n{"number":2}\n', encoding="utf-8")
    archive = tmp_path / "catalog.jsonl.zst"
    _compress(source, archive)
    source.unlink()
    with (
        open_catalog(archive) as stream,
        io.TextIOWrapper(stream, encoding="utf-8") as text,
    ):
        assert [json.loads(line) for line in text] == [{"title": "åäö"}, {"number": 2}]
    assert list(tmp_path.iterdir()) == [archive]


def test_catalog_import_does_not_load_harvesting():
    import subprocess
    import sys

    code = "import apps.publication.archives,sys; assert not any(x in sys.modules for x in ['apps.operations.harvest','apps.harvest.metadata_adapters.pxweb_v1.workflow','httpx','playwright'])"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_catalog_publication_rejects_unrelated_staging(tmp_path, monkeypatch):
    import subprocess

    from apps.publication.git import publish

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

    from apps.publication.candidates import verify_published

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    collection(tmp_path / "data")
    with pytest.raises(ValueError, match="committed"):
        verify_published(Path("data"), ["pxweb_v1"])


def test_workflow_dispatch_is_one_way_and_before_error_exit():
    root = ROOT
    catalog = (root / ".github/workflows/catalog.yml").read_text()
    assert "apps.operations.harvest" not in catalog
    assert "gh workflow run" not in catalog
    assert "schedule:" not in catalog
    for name in ("harvest.yml", "harvest-pxweb-v2.yml", "harvest-kolada.yml"):
        content = (root / ".github/workflows" / name).read_text()
        assert content.index("gh workflow run catalog.yml") > content.rindex(
            "apps.operations.harvest publish"
        )
        assert content.index("gh workflow run catalog.yml") < content.index(
            "apps.operations.outcome --adapter"
        )


def test_catalog_dispatch_is_skipped_while_a_catalog_run_is_queued():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        content = path.read_text(encoding="utf-8")
        if "gh workflow run catalog.yml" not in content:
            continue
        guard = content.index('IN("requested", "waiting", "pending", "queued")')
        assert guard < content.index("gh workflow run catalog.yml"), path.name


def test_cli_rejects_ignored_source_files(tmp_path, monkeypatch):
    import subprocess

    from apps.publication.candidates import verify_published

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


def test_interrupted_compression_preserves_archive_and_can_retry(tmp_path, monkeypatch):
    import apps.publication.archives as catalog

    source, archive = tmp_path / "catalog.jsonl", tmp_path / "catalog.jsonl.zst"
    source.write_bytes(b'{"dataset":1}\n')
    archive.write_bytes(b"previous archive")

    def fail(source, target, **kwargs):
        target.write(b"partial")
        raise OSError("interrupted compression")

    with monkeypatch.context() as patch:
        patch.setattr(catalog.shutil, "copyfileobj", fail)
        with pytest.raises(OSError, match="interrupted"):
            catalog._compress(source, archive)
    assert archive.read_bytes() == b"previous archive"
    assert not list(tmp_path.glob("*.tmp"))
    catalog._compress(source, archive)
    assert unpack(archive) == source.read_bytes()

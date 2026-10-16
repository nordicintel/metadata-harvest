"""Publication commits only the data it was asked to publish."""

import gzip

import pytest


def collection(root, content=b'{"trusted":true}\n'):
    path = root / "metadata/test__sv.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))
    return path


def test_publication_rejects_unrelated_staging(tmp_path, monkeypatch):
    import subprocess

    from apps.publication.git import publish

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    collection(tmp_path / "data/pxweb_v1")
    (tmp_path / "unrelated.txt").write_text("unrelated")
    git("add", "unrelated.txt")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="unrelated staged"):
        publish("pxweb_v1", data=tmp_path / "data/pxweb_v1")
    assert git("diff", "--cached", "--name-only").stdout.strip() == "unrelated.txt"

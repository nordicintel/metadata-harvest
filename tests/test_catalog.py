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
    assert first["build_counts"]["copied"] == 1
    assert first["stages"] == {
        "content_enrichment": "not_configured",
        "semantic_enrichment": "not_configured",
    }
    promote(data, candidate)
    target = data / "catalog/catalog.jsonl.gz"
    assert target.read_bytes() == source.read_bytes()
    second = build(data, candidate, adapters=["pxweb_v1"])
    assert second["build_counts"]["reused"] == 1
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
    target = data / "catalog/catalog.jsonl.gz"
    before = target.read_bytes()
    source.write_bytes(b"broken gzip")
    other = source.with_name("other.jsonl.gz")
    other.write_bytes(gzip.compress(b"{}\n", mtime=0))
    result = build(data, candidate, adapters=["pxweb_v1"])
    assert result["status"] == "completed_with_errors"
    promote(data, candidate)
    assert gzip.decompress(target.read_bytes()) == b'{}\n{"trusted":true}\n'
    assert before in target.read_bytes()
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
    (candidate / "catalog.jsonl.gz").write_bytes(b"tampered")
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
            "metadata_harvest.outcome --adapter"
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


def test_combined_assets_separate_dataset_and_codelist_records(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data, b'{"dataset":"sv"}\n')
    second = data / "pxweb_v2/metadata/scb__en.jsonl.gz"
    second.parent.mkdir(parents=True)
    second.write_bytes(gzip.compress(b'{"dataset":"en"}\n', mtime=0))
    codes = data / "pxweb_v2/codelists/scb__en.jsonl.gz"
    codes.parent.mkdir()
    codes.write_bytes(gzip.compress(b'{"codelist":{"id":"region"}}\n', mtime=0))
    build(data, candidate, adapters=["pxweb_v1", "pxweb_v2"])
    assert gzip.decompress((candidate / "catalog.jsonl.gz").read_bytes()) == (
        b'{"dataset":"sv"}\n{"dataset":"en"}\n'
    )
    assert gzip.decompress((candidate / "codelists.jsonl.gz").read_bytes()) == (
        b'{"codelist":{"id":"region"}}\n'
    )
    assert sorted(p.name for p in candidate.glob("*.jsonl.gz")) == [
        "catalog.jsonl.gz",
        "codelists.jsonl.gz",
    ]
    promote(data, candidate)
    first = (candidate / "catalog.jsonl.gz").read_bytes()
    build(data, candidate, adapters=["pxweb_v1", "pxweb_v2"])
    assert (candidate / "catalog.jsonl.gz").read_bytes() == first


def test_legacy_catalog_migrates_without_losing_failed_input(tmp_path):
    import hashlib

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    legacy = collection(data / "catalog")
    state = data / "catalog/state/current.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "accepted": {
                    "pxweb_v1/metadata/test__sv.jsonl.gz": hashlib.sha256(
                        legacy.read_bytes()
                    ).hexdigest()
                }
            }
        )
    )
    source.write_bytes(b"broken")
    result = build(data, candidate, adapters=["pxweb_v1"])
    assert result["counts"]["retained"] == 1
    promote(data, candidate)
    assert gzip.decompress((data / "catalog/catalog.jsonl.gz").read_bytes()) == (
        b'{"trusted":true}\n'
    )
    assert not legacy.exists()
    assert source.read_bytes() == b"broken"


def test_unavailable_previous_asset_blocks_build_instead_of_losing_records(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    source.write_bytes(b"broken")
    (data / "catalog/catalog.jsonl.gz").unlink()
    with pytest.raises(ValueError, match="previous|Previous"):
        build(data, candidate, adapters=["pxweb_v1"])


def test_corrupt_previous_asset_cannot_supply_retained_records(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    source.unlink()
    (data / "catalog/catalog.jsonl.gz").write_bytes(b"corrupt previous asset")
    with pytest.raises(ValueError, match="previous|Previous"):
        build(data, candidate, adapters=["pxweb_v1"])


def test_release_upload_is_completed_before_publication_and_supports_retry(
    tmp_path, monkeypatch
):
    import hashlib
    import subprocess

    import metadata_harvest.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    remote = {}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "abc123\n", "")
        if args[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(
                args,
                0 if remote else 1,
                json.dumps(remote) if remote else "",
                "" if remote else "Not Found (HTTP 404)",
            )
        if args[:3] == ["gh", "release", "create"]:
            remote.update(draft=True, assets=[])
        elif args[:3] == ["gh", "release", "upload"]:
            for name in args[4:]:
                if name.startswith("--"):
                    continue
                path = Path(name)
                remote["assets"].append(
                    {
                        "name": path.name,
                        "digest": "sha256:"
                        + hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        elif args[:3] == ["gh", "release", "edit"]:
            assert len(remote["assets"]) == 4
            remote["draft"] = False
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(catalog.subprocess, "run", run)
    assert hasattr(catalog, "publish_release")
    catalog.publish_release(data, candidate)
    assert not remote["draft"]
    assert {a["name"] for a in remote["assets"]} == {
        "catalog.jsonl.gz",
        "codelists.jsonl.gz",
        "current.json",
        "latest.json",
    }
    before = len([c for c in calls if c[:3] == ["gh", "release", "upload"]])
    catalog.publish_release(data, candidate)
    assert len([c for c in calls if c[:3] == ["gh", "release", "upload"]]) == before


def test_release_failure_leaves_catalog_untouched(tmp_path, monkeypatch):
    import subprocess

    import metadata_harvest.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="upload unavailable")

    monkeypatch.setattr(catalog.subprocess, "run", fail)
    assert hasattr(catalog, "publish_release")
    with pytest.raises(subprocess.CalledProcessError):
        catalog.publish_release(data, candidate)
    assert not (data / "catalog").exists()


def test_restore_downloads_only_assets_named_by_committed_state(tmp_path, monkeypatch):
    import subprocess

    import metadata_harvest.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    (data / "catalog/catalog.jsonl.gz").unlink()

    def run(args, **kwargs):
        assert args[:3] == ["gh", "release", "download"]
        assert (
            args[3]
            == json.loads((candidate / "state/current.json").read_text())["release"]
        )
        directory = Path(args[args.index("--dir") + 1])
        for name in ("catalog.jsonl.gz", "codelists.jsonl.gz"):
            (directory / name).write_bytes((candidate / name).read_bytes())
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(catalog.subprocess, "run", run)
    assert hasattr(catalog, "restore_assets")
    catalog.restore_assets(data)
    assert (data / "catalog/catalog.jsonl.gz").read_bytes() == (
        candidate / "catalog.jsonl.gz"
    ).read_bytes()


def test_unchanged_rebuild_keeps_release_and_published_report_identical(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    before = (candidate / "state/current.json").read_bytes()
    report = (candidate / "reports/latest.json").read_bytes()
    promote(data, candidate)
    build(data, candidate, adapters=["pxweb_v1"])
    assert (candidate / "state/current.json").read_bytes() == before
    assert (candidate / "reports/latest.json").read_bytes() == report


@pytest.mark.parametrize("missing", [False, True])
def test_legacy_collection_without_newline_cannot_join_records(tmp_path, missing):
    import hashlib

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data, b'{"first":1}')
    old = collection(data / "catalog", b'{"first":1}')
    state = data / "catalog/state/current.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "accepted": {
                    "pxweb_v1/metadata/test__sv.jsonl.gz": hashlib.sha256(
                        old.read_bytes()
                    ).hexdigest()
                }
            }
        )
    )
    if missing:
        source.unlink()
    sibling = source.with_name("z.jsonl.gz")
    sibling.write_bytes(gzip.compress(b'{"second":2}\n', mtime=0))
    with pytest.raises(ValueError, match="newline"):
        build(data, candidate, adapters=["pxweb_v1"])
    assert old.exists()
    assert not (candidate / "result.json").exists()


def test_release_rejects_stale_input_before_contacting_github(tmp_path, monkeypatch):
    import metadata_harvest.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    source.write_bytes(gzip.compress(b"{}\n", mtime=0))
    monkeypatch.setattr(
        catalog.subprocess, "run", lambda *a, **k: pytest.fail("network forbidden")
    )
    with pytest.raises(ValueError, match="Stale"):
        catalog.publish_release(data, candidate)


def test_restore_rejects_corruption_before_replacing_either_asset(
    tmp_path, monkeypatch
):
    import subprocess

    import metadata_harvest.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    (data / "catalog/catalog.jsonl.gz").unlink()
    before = (data / "catalog/codelists.jsonl.gz").read_bytes()

    def run(args, **kwargs):
        directory = Path(args[args.index("--dir") + 1])
        for name in ("catalog.jsonl.gz", "codelists.jsonl.gz"):
            (directory / name).write_bytes(b"corrupt")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(catalog.subprocess, "run", run)
    with pytest.raises(ValueError, match="checksum"):
        catalog.restore_assets(data)
    assert not (data / "catalog/catalog.jsonl.gz").exists()
    assert (data / "catalog/codelists.jsonl.gz").read_bytes() == before


def test_git_publication_commits_state_but_never_combined_payloads(
    tmp_path, monkeypatch
):
    import subprocess

    import metadata_harvest.publication as publication

    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    real_run = subprocess.run
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return real_run(["git", *args], check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text(ignore)
    git("add", ".gitignore")
    git("commit", "-m", "initial")
    catalog = tmp_path / "data/catalog"
    (catalog / "state").mkdir(parents=True)
    (catalog / "state/current.json").write_text("{}")
    for name in ("catalog.jsonl.gz", "codelists.jsonl.gz"):
        with (catalog / name).open("wb") as stream:
            stream.truncate(100 * 1024 * 1024)

    def run(args, **kwargs):
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, **kwargs)

    monkeypatch.setattr(publication.subprocess, "run", run)
    publication.publish("catalog state", data=catalog)
    tracked = git("ls-files").stdout.splitlines()
    assert "data/catalog/state/current.json" in tracked
    assert not any(name.endswith(".jsonl.gz") for name in tracked)


def test_release_digest_mismatch_never_publishes_or_promotes(tmp_path, monkeypatch):
    import subprocess

    import metadata_harvest.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "draft": True,
                        "assets": [
                            {"name": "catalog.jsonl.gz", "digest": "sha256:wrong"}
                        ],
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(catalog.subprocess, "run", run)
    with pytest.raises(ValueError, match="Release assets"):
        catalog.publish_release(data, candidate)
    assert not any(args[:3] == ["gh", "release", "edit"] for args in calls)
    assert not (data / "catalog").exists()

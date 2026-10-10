"""Offline catalog boundaries and failure isolation."""

import gzip
import json
from pathlib import Path

import pytest

from harvesting.catalog import build_legacy as build
from harvesting.catalog import promote


def unpack(path):
    from harvesting.catalog import open_catalog

    with open_catalog(path) as stream:
        return stream.read()


def collection(root, content=b'{"trusted":true}\n'):
    path = root / "pxweb_v1/metadata/test__sv.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))
    return path


def test_streaming_lines_without_extracted_files(tmp_path):
    import io

    from harvesting.catalog import _compress, open_catalog

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


def test_previous_7z_retains_failed_inputs_and_is_removed_only_at_promotion(tmp_path):
    import hashlib
    import subprocess

    from harvesting.catalog import ASSETS, sevenzip

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate)
    promote(data, candidate)
    catalog = data / "catalog"
    state_path = catalog / "state/current.json"
    state = json.loads(state_path.read_text())
    old_assets = {}
    for asset in ASSETS:
        member = asset.removesuffix(".zst")
        content = tmp_path / member
        content.write_bytes(unpack(catalog / asset))
        old = catalog / member.replace(".jsonl", ".7z")
        subprocess.run(
            [sevenzip(), "a", str(old), member],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        old_assets[old.name] = hashlib.sha256(old.read_bytes()).hexdigest()
        (catalog / asset).unlink()
    state["assets"] = old_assets
    state_path.write_text(json.dumps(state))
    source.write_bytes(b"failed update")
    report = build(data, candidate)
    assert report["build_counts"]["retained"] == 1
    assert unpack(candidate / ASSETS[0]) == b'{"trusted":true}\n'
    assert (catalog / "catalog.7z").exists()
    promote(data, candidate)
    assert not list(catalog.glob("*.7z"))
    assert unpack(catalog / ASSETS[0]) == b'{"trusted":true}\n'


def test_offline_trusted_copy_reuse_and_removal(tmp_path, monkeypatch):
    import socket

    import harvesting.validation as validation

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
    target = data / "catalog/catalog.jsonl.zst"
    assert unpack(target) == gzip.decompress(source.read_bytes())
    second = build(data, candidate, adapters=["pxweb_v1"])
    assert second["build_counts"]["reused"] == 1
    promote(data, candidate)
    source.write_bytes(gzip.compress(b"", mtime=0))
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    assert unpack(target) == b""


def test_corrupt_and_missing_retain_previous_other_collections_advance(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    target = data / "catalog/catalog.jsonl.zst"
    before = unpack(target)
    source.write_bytes(b"broken gzip")
    other = source.with_name("other.jsonl.gz")
    other.write_bytes(gzip.compress(b"{}\n", mtime=0))
    result = build(data, candidate, adapters=["pxweb_v1"])
    assert result["status"] == "completed_with_errors"
    promote(data, candidate)
    assert unpack(target) == b'{}\n{"trusted":true}\n'
    assert before in unpack(target)
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
    (candidate / "catalog.jsonl.zst").write_bytes(b"tampered")
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

    code = "import harvesting.catalog,sys; assert not any(x in sys.modules for x in ['harvesting.runner','harvesting.workflow','httpx','playwright'])"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_catalog_publication_rejects_unrelated_staging(tmp_path, monkeypatch):
    import subprocess

    from harvesting.publication import publish

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

    from harvesting.catalog import verify_published

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    collection(tmp_path / "data")
    with pytest.raises(ValueError, match="committed"):
        verify_published(Path("data"), ["pxweb_v1"])


def test_workflow_dispatch_is_one_way_and_before_error_exit():
    root = Path(__file__).resolve().parents[1]
    catalog = (root / ".github/workflows/catalog.yml").read_text()
    assert "harvesting.runner" not in catalog
    assert "gh workflow run" not in catalog
    assert "schedule:" not in catalog
    for name in ("harvest.yml", "harvest-pxweb-v2.yml", "harvest-kolada.yml"):
        content = (root / ".github/workflows" / name).read_text()
        assert content.index("gh workflow run catalog.yml") > content.rindex(
            "harvesting.runner publish"
        )
        assert content.index("gh workflow run catalog.yml") < content.index(
            "harvesting.outcome --adapter"
        )


def test_cli_rejects_ignored_source_files(tmp_path, monkeypatch):
    import subprocess

    from harvesting.catalog import verify_published

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
    assert unpack(candidate / "catalog.jsonl.zst") == (
        b'{"dataset":"sv"}\n{"dataset":"en"}\n'
    )
    assert unpack(candidate / "codelists.jsonl.zst") == (
        b'{"codelist":{"id":"region"}}\n'
    )
    assert sorted(p.name for p in candidate.glob("*.zst")) == [
        "catalog.jsonl.zst",
        "codelists.jsonl.zst",
    ]
    promote(data, candidate)
    first = (candidate / "catalog.jsonl.zst").read_bytes()
    build(data, candidate, adapters=["pxweb_v1", "pxweb_v2"])
    assert (candidate / "catalog.jsonl.zst").read_bytes() == first


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
    assert unpack(data / "catalog/catalog.jsonl.zst") == (b'{"trusted":true}\n')
    assert not legacy.exists()
    assert source.read_bytes() == b"broken"


def test_unavailable_previous_asset_blocks_build_instead_of_losing_records(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    source.write_bytes(b"broken")
    (data / "catalog/catalog.jsonl.zst").unlink()
    with pytest.raises(ValueError, match="previous|Previous"):
        build(data, candidate, adapters=["pxweb_v1"])


def test_corrupt_previous_asset_cannot_supply_retained_records(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    source.unlink()
    (data / "catalog/catalog.jsonl.zst").write_bytes(b"corrupt previous asset")
    with pytest.raises(ValueError, match="previous|Previous"):
        build(data, candidate, adapters=["pxweb_v1"])


def test_unchanged_rebuild_keeps_state_archives_and_report_identical(tmp_path):
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    before = (candidate / "state/current.json").read_bytes()
    report = (candidate / "reports/latest.json").read_bytes()
    promote(data, candidate)
    build(data, candidate, adapters=["pxweb_v1"])
    assert (candidate / "state/current.json").read_bytes() == before
    assert (candidate / "reports/latest.json").read_bytes() == report


def test_codelist_change_recompresses_only_codelists(tmp_path, monkeypatch):
    import harvesting.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    codes = data / "pxweb_v1/codelists/test__sv.jsonl.gz"
    codes.parent.mkdir()
    codes.write_bytes(gzip.compress(b'{"codelist":1}\n', mtime=0))
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    before = (candidate / "catalog.jsonl.zst").read_bytes()
    compressed = []
    compress = catalog._compress

    def record(source, archive):
        compressed.append(archive.name)
        compress(source, archive)

    monkeypatch.setattr(catalog, "_compress", record)
    codes.write_bytes(gzip.compress(b'{"codelist":2}\n', mtime=0))
    build(data, candidate, adapters=["pxweb_v1"])
    assert compressed == ["codelists.jsonl.zst"]
    assert (candidate / "catalog.jsonl.zst").read_bytes() == before
    assert unpack(candidate / "codelists.jsonl.zst") == b'{"codelist":2}\n'


def test_identical_inputs_build_identical_archives_in_different_directories(tmp_path):
    data = tmp_path / "data"
    collection(data)
    first, second = tmp_path / "first", tmp_path / "second"
    build(data, first, adapters=["pxweb_v1"])
    build(data, second, adapters=["pxweb_v1"])
    for name in ("catalog.jsonl.zst", "codelists.jsonl.zst"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_size_limit_rejects_candidate_without_changing_accepted_catalog(
    tmp_path, monkeypatch
):
    import harvesting.catalog as catalog

    data, candidate = tmp_path / "data", tmp_path / "candidate"
    source = collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    before = (data / "catalog/catalog.jsonl.zst").read_bytes()
    source.write_bytes(gzip.compress(b'{"changed":true}\n', mtime=0))
    check = catalog.check_collection_sizes
    monkeypatch.setattr(
        catalog, "check_collection_sizes", lambda path: check(path, limit=1)
    )
    with pytest.raises(ValueError, match="file limit"):
        build(data, candidate, adapters=["pxweb_v1"])
    assert (data / "catalog/catalog.jsonl.zst").read_bytes() == before
    assert not (candidate / "result.json").exists()


def test_combined_archives_are_committed_with_state_and_reports(tmp_path, monkeypatch):
    import subprocess

    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    data, candidate = tmp_path / "data", tmp_path / "candidate"
    collection(data)
    build(data, candidate, adapters=["pxweb_v1"])
    promote(data, candidate)
    (tmp_path / ".gitignore").write_text(ignore)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init"], check=True, capture_output=True)
    subprocess.run(["git", "add", "data/catalog"], check=True, capture_output=True)
    tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
    assert tracked == [
        "data/catalog/catalog.jsonl.zst",
        "data/catalog/codelists.jsonl.zst",
        "data/catalog/reports/latest.json",
        "data/catalog/state/current.json",
    ]


def test_interrupted_compression_preserves_archive_and_can_retry(tmp_path, monkeypatch):
    import harvesting.catalog as catalog

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

"""StatDCAT assembly is offline, deterministic, scoped and failure preserving."""

import json

import pytest

from harvesting.accepted import write_metadata
from tests.test_catalog import unpack
from tests.test_statdcat import legacy


def test_catalog_builds_models_reuses_bytes_and_filters_scb(tmp_path, monkeypatch):
    import socket

    from harvesting.catalog import build, promote
    from harvesting.portal import import_snapshot
    from tests.test_portal import record

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("network")
    )
    data = tmp_path / "data"
    candidate = tmp_path / "candidate"
    write_metadata(data / "pxweb_v2", [legacy("sv"), legacy("en")], adapter="pxweb_v2")
    source = tmp_path / "portal.jsonl"
    source.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    import_snapshot(source, data / "data_eu")
    result = build(data, candidate, adapters=["pxweb_v2"])
    lines = [
        json.loads(line)
        for line in unpack(candidate / "catalog.jsonl.zst").splitlines()
    ]
    assert len(lines) == 1
    assert len(lines[0]["dcat:distribution"]) == 2
    assert result["counts"]["datasets"] == 1
    promote(data, candidate)
    before = (data / "catalog/catalog.jsonl.zst").read_bytes()
    second = build(data, candidate, adapters=["pxweb_v2"])
    assert second["build_counts"]["reused_groups"] == 1
    assert (candidate / "catalog.jsonl.zst").read_bytes() == before
    config = tmp_path / "catalog.toml"
    config.write_text("include_scb_pxweb_v1 = true\n", encoding="utf-8")
    build(data, candidate, adapters=["pxweb_v2"])
    lines = [
        json.loads(line)
        for line in unpack(candidate / "catalog.jsonl.zst").splitlines()
    ]
    assert len(lines[0]["dcat:distribution"]) == 3


def test_failed_input_retains_group_and_stale_configuration_blocks_promotion(tmp_path):
    from harvesting.catalog import build, promote

    data = tmp_path / "data"
    candidate = tmp_path / "candidate"
    write_metadata(data / "pxweb_v2", [legacy()], adapter="pxweb_v2")
    build(data, candidate, adapters=["pxweb_v2"])
    promote(data, candidate)
    original = unpack(data / "catalog/catalog.jsonl.zst")
    next((data / "pxweb_v2/metadata").glob("*.gz")).write_bytes(b"corrupt")
    result = build(data, candidate, adapters=["pxweb_v2"])
    assert result["errors"]
    assert unpack(candidate / "catalog.jsonl.zst") == original
    (tmp_path / "catalog.toml").write_text("include_scb_pxweb_v1 = true\n")
    with pytest.raises(ValueError, match="Stale"):
        promote(data, candidate)


def test_committed_failed_input_is_restored_while_sibling_update_advances(tmp_path):
    import subprocess

    from harvesting.catalog import build, promote

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    data = tmp_path / "data"
    candidate = tmp_path / "candidate"
    v2 = legacy()
    hvd = legacy(provider="scb_hvd", code="HVD1")
    hvd["extension"]["pxweb_v1"] = hvd["extension"].pop("pxweb_v2")
    write_metadata(data / "pxweb_v2", [v2], adapter="pxweb_v2")
    write_metadata(data / "pxweb_v1", [hvd], adapter="pxweb_v1")
    git("add", "data")
    git("commit", "-m", "accepted inputs")
    build(data, candidate, adapters=["pxweb_v1", "pxweb_v2"])
    promote(data, candidate)
    hvd["label"] = "Updated HVD"
    write_metadata(data / "pxweb_v1", [hvd], adapter="pxweb_v1")
    next((data / "pxweb_v2/metadata").glob("*.gz")).write_bytes(b"bad")
    report = build(data, candidate, adapters=["pxweb_v1", "pxweb_v2"])
    models = [
        json.loads(line)
        for line in unpack(candidate / "catalog.jsonl.zst").splitlines()
    ]
    assert len(models) == 2
    assert any(d["dct:title"] == {"sv": ["Updated HVD"]} for d in models)
    assert report["errors"][0]["retained_blob"]


def test_unrelated_portal_change_reuses_assembled_owner(tmp_path):
    from harvesting.catalog import build, promote
    from harvesting.portal import import_snapshot
    from tests.test_portal import record

    data = tmp_path / "data"
    candidate = tmp_path / "candidate"
    write_metadata(data / "pxweb_v2", [legacy()], adapter="pxweb_v2")
    source = tmp_path / "portal.jsonl"
    source.write_text(json.dumps(record()) + "\n")
    import_snapshot(source, data / "data_eu")
    build(data, candidate, adapters=["pxweb_v2"])
    promote(data, candidate)
    unrelated = record()
    unrelated.update(
        id="unrelated",
        identifier=["UNRELATED"],
        distributions=[],
        resource="https://example.org/unrelated",
    )
    source.write_text(json.dumps(record()) + "\n" + json.dumps(unrelated) + "\n")
    import_snapshot(source, data / "data_eu")
    report = build(data, candidate, adapters=["pxweb_v2"])
    assert report["build_counts"]["reused_groups"] == 1


def test_previous_profile_reads_and_blob_recovery_do_not_require_production_cutover(
    tmp_path,
):
    import gzip
    import subprocess

    from harvesting.accepted import load_contributions
    from harvesting.catalog import build, promote
    from harvesting.models import PROFILE1_CONTEXT, Dataset

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    data = tmp_path / "data"
    candidate = tmp_path / "fixture-candidate"
    write_metadata(data / "pxweb_v2", [legacy()], adapter="pxweb_v2")
    path = next((data / "pxweb_v2/metadata").glob("*.gz"))
    previous = json.loads(gzip.decompress(path.read_bytes()))
    previous["ni:profileVersion"] = "1.0.0"
    previous["@context"] = PROFILE1_CONTEXT
    path.write_bytes(gzip.compress((json.dumps(previous) + "\n").encode(), mtime=0))
    before = path.read_bytes()
    assert list(load_contributions(path.parent)) == [legacy()]
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        Dataset.from_jsonld(previous)
    git("add", "data")
    git("commit", "-m", "previous-profile accepted input")
    build(data, candidate, adapters=["pxweb_v2"])
    promote(data, candidate)
    state_path = data / "catalog/state/current.json"
    state = json.loads(state_path.read_text())
    state["profile_version"] = "1.0.0"
    state_path.write_text(json.dumps(state))
    path.write_bytes(b"failed current input")
    report = build(data, candidate, adapters=["pxweb_v2"])
    assert report["errors"][0]["retained_blob"]
    assert report["build_counts"]["reused_groups"] == 0
    assert (
        json.loads(unpack(candidate / "catalog.jsonl.zst"))["ni:profileVersion"]
        == "2.0.0"
    )

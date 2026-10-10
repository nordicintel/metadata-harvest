"""Production promotion gates exercised only against temporary fixture catalogs."""

import shutil

import pytest

from apps.catalog.assembly import build
from apps.harvest.accepted import write_metadata
from apps.paths import CONFIG
from apps.publication.candidates import promote, verify_candidate
from tests.model.test_statdcat import legacy


def candidate(tmp_path):
    data, output, config = (
        tmp_path / "data",
        tmp_path / "candidate",
        tmp_path / "config",
    )
    shutil.copytree(CONFIG, config)
    write_metadata(data / "pxweb_v2", [legacy()], adapter="pxweb_v2")
    build(data, output, adapters=["pxweb_v2"], config_dir=config)
    return data, output, config


def test_tampered_candidate_cannot_be_promoted(tmp_path):
    data, output, config = candidate(tmp_path)
    (output / "catalog.jsonl.zst").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        promote(data, output, config_dir=config)
    assert not (data / "catalog/catalog.jsonl.zst").exists()


def test_explicit_configuration_change_invalidates_candidate(tmp_path):
    data, output, config = candidate(tmp_path)
    (config / "catalog.toml").write_text(
        "include_scb_pxweb_v1 = true\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Stale"):
        verify_candidate(data, output, config_dir=config)


def test_size_failure_preserves_accepted_archive(tmp_path, monkeypatch):
    import apps.catalog.assembly as assembly

    data, output, config = candidate(tmp_path)
    promote(data, output, config_dir=config)
    before = (data / "catalog/catalog.jsonl.zst").read_bytes()
    original = assembly.check_collection_sizes
    monkeypatch.setattr(
        assembly, "check_collection_sizes", lambda path: original(path, limit=1)
    )
    with pytest.raises(ValueError, match="file limit"):
        build(data, output, adapters=["pxweb_v2"], config_dir=config)
    assert (data / "catalog/catalog.jsonl.zst").read_bytes() == before


def test_pre_cutover_candidate_without_dependencies_is_rejected(tmp_path):
    import json

    data, output, config = candidate(tmp_path)
    path = output / "result.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("dependencies")
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="Stale catalog candidate"):
        verify_candidate(data, output, config_dir=config)

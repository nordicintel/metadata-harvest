"""Grouped publication must retain identities, contents and failed replacements."""

import gzip
import json

import pytest

from metadata_harvest.storage import filename, read, write
from tests.test_workflow import dataset, record


def test_compressed_collections_preserve_content_and_exact_bytes(tmp_path):
    from metadata_harvest.storage import JsonlStore

    row = {
        "provider_code": "scb",
        "dataset_code": "A",
        "language": "sv",
        "label": "Åäö",
    }
    store = JsonlStore(tmp_path, compressed=True)
    store.records[filename(row)] = row
    store.save()
    path = next(tmp_path.glob("*.jsonl.gz"))
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    assert json.loads(gzip.decompress(before)) == row
    assert JsonlStore(tmp_path).records == store.records
    store.save()
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified
    row["label"] = float("nan")
    with pytest.raises(ValueError):
        store.save()
    assert path.read_bytes() == before


def test_jsonl_store_accepts_explicit_codelist_identity(tmp_path):
    from metadata_harvest.storage import JsonlStore

    def identify(doc):
        return {
            "provider_code": "scb",
            "dataset_code": doc["id"],
            "language": doc["language"],
        }

    doc = {"id": "vs_Region", "language": "sv", "values": []}
    store = JsonlStore(tmp_path, identify=identify)
    store.records[filename(identify(doc))] = doc
    store.save()
    assert list(JsonlStore(tmp_path, identify=identify).records.values()) == [doc]


def test_jsonl_store_groups_languages_and_is_stable(tmp_path):
    from metadata_harvest.storage import JsonlStore

    rows = [record("z"), record("a"), {**record("b"), "language": "en"}]
    store = JsonlStore(tmp_path)
    for row in rows:
        store.records[filename(row)] = row
    store.save()
    paths = sorted(tmp_path.glob("*.jsonl"))
    assert len(paths) == 2
    original = {path.name: path.read_bytes() for path in paths}
    loaded = JsonlStore(tmp_path)
    assert loaded.records == store.records
    loaded.records = dict(reversed(list(loaded.records.items())))
    loaded.save()
    assert {path.name: path.read_bytes() for path in paths} == original
    assert not list(tmp_path.glob("*.json"))


def test_jsonl_migration_preserves_every_document_before_removing_legacy(tmp_path):
    from metadata_harvest.storage import JsonlStore

    rows = [dataset(record("a")), dataset(record("b"))]
    for row in rows:
        write(tmp_path / filename(row["extension"]["metadata_harvest"]), row)
    store = JsonlStore(tmp_path)
    store.save()
    assert list(JsonlStore(tmp_path).records.values()) == rows
    assert len(list(tmp_path.glob("*.jsonl"))) == 1
    assert not list(tmp_path.glob("*.json"))


def test_invalid_jsonl_replacement_preserves_previous_group(tmp_path):
    from metadata_harvest.storage import JsonlStore

    row = record()
    store = JsonlStore(tmp_path)
    store.records[filename(row)] = row
    store.save()
    path = next(tmp_path.glob("*.jsonl"))
    previous = path.read_bytes()
    store.records[filename(row)] = {**row, "bad": float("nan")}
    with pytest.raises(ValueError):
        store.save()
    assert path.read_bytes() == previous


def test_conflicting_legacy_and_jsonl_are_rejected_without_deletion(tmp_path):
    from metadata_harvest.storage import JsonlStore

    row = record()
    store = JsonlStore(tmp_path)
    store.records[filename(row)] = row
    store.save()
    legacy = tmp_path / filename(row)
    write(legacy, {**row, "title": "Conflict"})
    with pytest.raises(ValueError, match="Conflicting"):
        JsonlStore(tmp_path)
    assert read(legacy)["title"] == "Conflict"


def test_failure_in_later_language_does_not_replace_earlier_group(tmp_path):
    from metadata_harvest.storage import JsonlStore

    store = JsonlStore(tmp_path)
    for language in ("en", "sv"):
        row = {**record(language), "language": language}
        store.records[filename(row)] = row
    store.save()
    original = {path.name: path.read_bytes() for path in tmp_path.glob("*.jsonl")}
    for row in store.records.values():
        row["title"] = "Changed"
        if row["language"] == "sv":
            row["bad"] = float("nan")
    with pytest.raises(ValueError):
        store.save()
    assert {
        path.name: path.read_bytes() for path in tmp_path.glob("*.jsonl")
    } == original


def test_legacy_document_can_be_updated_in_place_during_migration(tmp_path):
    from metadata_harvest.storage import JsonlStore

    row = record()
    write(tmp_path / filename(row), row)
    store = JsonlStore(tmp_path)
    store.records[filename(row)]["title"] = "Refreshed title"
    store.save()
    assert JsonlStore(tmp_path).records[filename(row)]["title"] == "Refreshed title"

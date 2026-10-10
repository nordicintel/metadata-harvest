"""Registry isolation, conversion and unavailable adapter boundaries."""

import json

import pytest

from tests.test_workflow import dataset, record


def test_registry_separates_ready_adapters_from_prepared_placeholders():
    from harvesting.registry import ADAPTERS, get_adapter

    assert {name for name, spec in ADAPTERS.items() if spec.available} == {
        "pxweb_v1",
        "pxweb_v2",
        "kolada",
        "uka",
        "sdb",
        "trafa",
        "entrysoft",
        "smhi_emissions",
        "tillvaxtverket",
        "dataanalys",
        "riksbank",
        "forsakringskassan",
    }
    assert all(spec.available for spec in ADAPTERS.values())
    with pytest.raises(ValueError):
        get_adapter("../outside")


def test_conversion_is_staged_and_preserves_sources_and_identity(tmp_path):
    from harvesting.migration import convert_collections
    from harvesting.storage import JsonlStore, filename

    source, target = tmp_path / "source", tmp_path / "candidate"
    old = JsonlStore(source / "metadata")
    row = record()
    old.records[filename(row)] = dataset(row)
    old.save()
    before = {p.name: p.read_bytes() for p in (source / "metadata").iterdir()}
    result = convert_collections(source, target, adapter="pxweb_v1")
    assert result["records"] == 1
    assert not (
        target / "reports/conversion.json"
    ).exists()  # Evidence is returned, not added to production state.
    assert {p.name: p.read_bytes() for p in (source / "metadata").iterdir()} == before
    assert len(list((target / "metadata").glob("*.jsonl.gz"))) == 1
    assert not list((target / "metadata").glob("*.jsonl"))
    doc = next(iter(JsonlStore(target / "metadata").records.values()))
    assert doc["extension"]["nordicintel"]["dataset_code"] == row["dataset_code"]
    assert doc["id"] == dataset(row)["id"]
    with pytest.raises(FileExistsError):
        convert_collections(source, target, adapter="pxweb_v1")


def test_invalid_conversion_never_creates_a_publishable_candidate(tmp_path):
    from harvesting.migration import convert_collections

    root = tmp_path / "source" / "metadata"
    root.mkdir(parents=True)
    doc = dataset(record())
    doc["size"] = [55]
    (root / "source.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
    target = tmp_path / "candidate"
    with pytest.raises(ValueError):
        convert_collections(root.parent, target, adapter="pxweb_v1")
    assert not target.exists()


def test_refresh_listing_does_not_replace_accepted_metadata_title():
    from harvesting.mapping import map_metadata, source_record
    from harvesting.storage import refresh_identity

    row = record()
    doc = map_metadata(dataset(row), row, "pxweb_v1")
    doc["label"] = "Provider metadata title"
    refresh_identity(doc, {**row, "title": "New catalogue title"})
    assert doc["label"] == "Provider metadata title"
    assert source_record(doc)["title"] == "New catalogue title"


def test_listing_refresh_updates_canonical_routes_and_paths_without_replacing_title():
    from harvesting.storage import refresh_identity, validate_dataset

    row = record()
    doc = dataset(row)
    doc["label"] = "Accepted source title"
    moved = {
        **row,
        "api_url": "https://example.test/new/[table]",
        "web_url": "https://example.test/new-page",
        "category_path": [{"id": "N", "name": "New theme"}],
        "title": "Listing title",
    }
    refresh_identity(doc, moved)
    shared = doc["extension"]["nordicintel"]
    assert shared["data_url"] == "https://example.test/new/%5Btable%5D"
    assert shared["metadata_url"] == shared["data_url"]
    assert shared["source_url"] == moved["web_url"]
    assert shared["paths"] == [[{"id": "N", "label": "New theme"}]]
    assert doc["label"] == "Accepted source title"
    assert doc["updated"] == row["published"]
    validate_dataset(doc, moved)
    refresh_identity(doc, {**moved, "category_path": [], "web_url": ""})
    assert "paths" not in shared and "source_url" not in shared

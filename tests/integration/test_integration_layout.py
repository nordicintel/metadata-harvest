"""Registry isolation, explicit conversion and accepted source refresh."""

from apps.paths import ROOT
from tests.harvest.test_workflow import dataset, record


def test_every_adapter_has_a_folder():
    from apps.harvest.metadata_adapters import ADAPTERS

    assert set(ADAPTERS) == {
        "pxweb_v1",
        "pxweb_v2",
        "kolada",
        "uka",
        "sdb",
        "trafa",
        "entryscape",
        "smhi_emissions",
        "tillvaxtverket",
        "dataanalys",
        "riksbank",
        "forsakringskassan",
    }
    for name in ADAPTERS:
        assert (ROOT / "apps/harvest/metadata_adapters" / name / "parse.py").is_file()


def test_listing_refresh_updates_canonical_routes_and_paths_without_replacing_title():
    from apps.harvest.metadata_adapters.pxweb_v1.mapping import source_record
    from apps.harvest.storage import refresh_identity

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
    assert source_record(doc)["title"] == moved["title"]
    assert doc["updated"] == row["published"]
    refresh_identity(doc, {**moved, "category_path": [], "web_url": ""})
    assert "paths" not in shared and "source_url" not in shared

"""Shared contracts preserve source meaning while rejecting invalid cubes."""

from copy import deepcopy

import pytest

from tests.test_pxweb_v2 import metadata, record


def test_native_mapping_produces_shared_identity_and_preserves_source_fields():
    from metadata_harvest.mapping import map_metadata
    from metadata_harvest.validation import validate_metadata

    native = metadata()
    native["dimension"]["Region"]["extension"].update(
        elimination=True, eliminationValueCode="01", show="value"
    )
    native["dimension"]["Region"]["category"]["unit"] = {
        "01": {"base": "people", "decimals": 0, "custom": "source unit detail"}
    }
    native["extension"]["contact"] = [{"name": "Contact", "mail": "a@example.org"}]
    original = deepcopy(native)
    result = map_metadata(native, record(), "pxweb_v2")
    validate_metadata(result, adapter="pxweb_v2")
    assert native == original
    assert result["extension"]["nordicintel"]["dataset_code"] == "T1"
    assert result["updated"] == "2026-10-01"
    assert result["label"] == native["label"]
    assert result["extension"]["pxweb_v2"]["source_record"] == record()
    assert result["extension"]["pxweb_v2"]["updated_original"] == native["updated"]
    assert result["extension"]["nordicintel"]["contacts"][0]["email"] == "a@example.org"
    dimension = result["dimension"]["Region"]
    assert dimension["extension"]["nordicintel"]["elimination_value"] == "01"
    assert dimension["category"]["unit"]["01"] == {"label": "people", "decimals": 0}
    assert dimension["extension"]["pxweb_v2"]["categories"]["01"]["unit"] == {
        "custom": "source unit detail"
    }
    assert (
        dimension["extension"]["pxweb_v2"]["codelists"]
        == native["dimension"]["Region"]["extension"]["codelists"]
    )


@pytest.mark.parametrize(
    "defect",
    ["size", "index", "role", "elimination", "hierarchy", "observations", "status"],
)
def test_shared_validation_rejects_semantic_defects(defect):
    from metadata_harvest.mapping import map_metadata
    from metadata_harvest.validation import validate_metadata

    result = map_metadata(metadata(), record(), "pxweb_v2")
    dimension = result["dimension"]["Region"]
    if defect == "size":
        result["size"] = [3]
    elif defect == "index":
        dimension["category"]["index"] = {"01": 0, "02": 0}
    elif defect == "role":
        result["role"] = {"geo": ["Region"], "metric": ["Region"]}
    elif defect == "elimination":
        dimension.setdefault("extension", {})["nordicintel"] = {
            "elimination_value": "missing"
        }
    elif defect == "hierarchy":
        dimension["category"]["child"] = {"01": ["02"], "02": ["01"]}
    elif defect == "observations":
        result["value"] = [1, 2]
    else:
        result["status"] = "p"
    with pytest.raises(ValueError):
        validate_metadata(result, adapter="pxweb_v2")


def test_schema_resources_are_owned_here_and_fragment_checks_selection():
    from metadata_harvest.validation import schema, validate_retrieval

    assert "metadata-harvest" in schema("dataset-metadata")["$id"]
    fragment = {
        "id": ["x"],
        "size": [2],
        "dimension": {"x": {"category": {"index": ["b", "a"]}}},
        "value": {"1": 0},
        "status": {"0": ".."},
    }
    validate_retrieval(fragment, selection={"x": ["a", "b"]})
    with pytest.raises(ValueError):
        validate_retrieval(fragment, selection={"x": ["a"]})
    fragment["value"]["2"] = 4
    with pytest.raises(ValueError):
        validate_retrieval(fragment, selection={"x": ["a", "b"]})


def test_mapping_encodes_source_url_without_double_encoding():
    from metadata_harvest.mapping import map_metadata
    from tests.test_workflow import dataset, record

    row = record()
    row["web_url"] = "https://example.org/%C3%A5/[published].px"
    doc = map_metadata(dataset(row), row, "pxweb_v1")
    assert (
        doc["extension"]["nordicintel"]["source_url"]
        == "https://example.org/%C3%A5/%5Bpublished%5D.px"
    )
    assert doc["extension"]["pxweb_v1"]["source_record"] == row


def test_v2_time_unit_maps_shared_vocabulary_without_changing_catalogue():
    from metadata_harvest.mapping import map_metadata

    row = record()
    row["catalogue"]["timeUnit"] = "Annual"
    doc = map_metadata(metadata(), row, "pxweb_v2")
    assert doc["extension"]["nordicintel"]["time_unit"] == "annual"
    assert doc["extension"]["pxweb_v2"]["source_record"] == row

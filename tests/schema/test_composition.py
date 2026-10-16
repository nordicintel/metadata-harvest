"""Per-language worker documents merge into one multilingual record."""

import pytest

from schema.composition import merge
from tests.support.worker_document import legacy


def test_languages_merge_into_maps_and_conflicts_are_retained():
    swedish, english = legacy("sv"), legacy("en")
    english["dimension"]["Region"]["category"]["label"] = {"01": "Ett", "00": "Alla"}
    english["extension"]["nordicintel"]["first_period"] = "2019"
    english["extension"]["nordicintel"]["doc_url"] = "https://example.org/doc"
    merged = merge([english, swedish])
    assert merged["label"] == {"en": "Table en", "sv": "Table sv"}
    assert merged["id"] == ["Region", "Tid"] and merged["size"] == [2, 2]
    region = merged["dimension"]["Region"]
    assert region["label"] == {"en": "Region", "sv": "Region"}
    assert region["category"]["label"] == {
        "en": {"01": "Ett", "00": "Alla"},
        "sv": {"01": "One", "00": "All"},
    }
    assert region["category"]["index"] == {"01": 0, "00": 1}
    shared = merged["extension"]["nordicintel"]
    assert shared["languages"] == ["en", "sv"]
    assert shared["first_period"] == "2019"
    assert shared["doc_url"] == "https://example.org/doc"
    assert shared["conflicts"] == {"nordicintel/first_period": ["2019", "2020"]}
    assert merged["extension"]["pxweb_v2"] == {
        "max_cells": 1000,
        "source_record": {"id": "T1"},
    }


def test_different_datasets_do_not_merge():
    with pytest.raises(ValueError, match="different datasets"):
        merge([legacy("sv", "T1"), legacy("sv", "T2")])

"""A minimal worker document used as a fixture across test areas."""


def legacy(language="sv", code="T1", provider="scb"):
    return {
        "class": "dataset",
        "version": "2.0",
        "label": "Table " + language,
        "id": ["Region", "Tid"],
        "size": [2, 2],
        "value": [],
        "role": {"geo": ["Region"], "time": ["Tid"]},
        "dimension": {
            "Region": {
                "label": "Region",
                "category": {
                    "index": {"01": 0, "00": 1},
                    "label": {"01": "One", "00": "All"},
                    "unit": {"01": {"label": "people", "decimals": 0}},
                    "custom": {"01": {"flag": False}},
                },
                "extension": {
                    "nordicintel": {"elimination": False},
                    "pxweb_v2": {"new": 0},
                },
            },
            "Tid": {"label": "Time", "category": {"index": ["2020", "2021"]}},
        },
        "extension": {
            "nordicintel": {
                "provider_code": provider,
                "dataset_code": code,
                "language": language,
                "first_period": "2020",
                "last_period": "2021",
                "time_unit": "annual",
                "official_statistics": True,
                "discontinued": False,
                "data_url": "https://example.org/data",
                "metadata_url": "https://example.org/meta",
                "new_information": {"zero": 0, "empty": [], "false": False},
            },
            "pxweb_v2": {"max_cells": 1000, "source_record": {"id": code}},
        },
    }

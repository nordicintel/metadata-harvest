"""The registry loads the three configuration files once and validates them."""

import json
from copy import deepcopy

import pytest

import registry

ADAPTERS = {
    "configuration_version": 1,
    "adapters": {
        "alpha": {
            "title": {"en": ["Alpha"]},
            "description": {"en": ["Alpha API"]},
            "parameters": {
                "languages": {"type": "array", "required": True},
                "rate_limit": {"type": "number"},
            },
            "defaults": {"languages": ["sv"], "rate_limit": 1},
        },
        "beta": {"title": {"en": ["Beta"]}, "description": {"en": ["Beta API"]}},
    },
}
ORGANISATIONS = {
    "configuration_version": 1,
    "organisations": {
        "one": {"id": "urn:nordicintel:agent:one", "name": {"sv": ["Ett"]}},
        "two": {"id": "urn:nordicintel:agent:two", "name": {"sv": ["Två"]}},
    },
}
CATALOGS = {
    "configuration_version": 1,
    "services": {
        "s:alpha:a": {
            "id": "s:alpha:a",
            "adapter": "alpha",
            "source_code": "a",
            "endpoint_url": "https://a.example/api",
            "publisher": "one",
            "order": 1,
            "options": {"rate_limit": 0.5},
        },
        "s:alpha:b": {
            "id": "s:alpha:b",
            "adapter": "alpha",
            "source_code": "b",
            "endpoint_url": "https://b.example/api",
            "publisher": "two",
            "order": 0,
            "options": {"languages": ["sv", "en"]},
        },
        "s:beta:shared": {
            "id": "s:beta:shared",
            "adapter": "beta",
            "source_code": "shared",
            "endpoint_url": "https://shared.example/",
            "publisher": None,
            "order": 0,
        },
    },
    "catalogs": {
        "c:a": {
            "id": "c:a",
            "key": "a",
            "title": {"sv": ["Katalog A"]},
            "publisher": "one",
            "services": ["s:alpha:a"],
            "description": {"sv": ["Beskrivning A", "mer"]},
        },
        "c:x": {
            "id": "c:x",
            "key": "x",
            "title": {"sv": ["X"]},
            "publisher": "one",
            "services": ["s:beta:shared"],
        },
        "c:y": {
            "id": "c:y",
            "key": "y",
            "title": {"sv": ["Y"]},
            "publisher": "two",
            "services": ["s:beta:shared"],
        },
    },
}


def write(root, adapters=ADAPTERS, organisations=ORGANISATIONS, catalogs=CATALOGS):
    for name, document in (
        ("adapters.json", adapters),
        ("organisations.json", organisations),
        ("catalogs.json", catalogs),
    ):
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    return root


def test_bindings_are_ordered_with_merged_options_and_catalog_labels(tmp_path):
    loaded = registry.load(write(tmp_path))
    assert loaded.adapters == ("alpha", "beta")
    b, a = loaded.bindings("alpha")
    assert (a.source_code, b.source_code) == ("a", "b")
    assert a.options == {"languages": ["sv"], "rate_limit": 0.5}
    assert b.options == {"languages": ["sv", "en"], "rate_limit": 1}
    assert a.endpoint_url == "https://a.example/api"
    assert (a.label, a.description) == ("Katalog A", {"sv": "Beskrivning A"})
    assert loaded.owner(a).name == {"sv": ["Ett"]}
    [shared] = loaded.bindings("beta")
    assert (shared.label, shared.description) == ("shared.example", {})
    assert loaded.owner(shared) is None
    with pytest.raises(ValueError, match="Unknown adapter"):
        loaded.bindings("gamma")


@pytest.mark.parametrize(
    "damage,message",
    [
        ("missing_required", "Missing alpha option: languages"),
        ("wrong_type", "Invalid alpha option: rate_limit"),
        ("unknown_adapter", "Unknown service adapter"),
        ("unknown_publisher", "Unknown service organisation"),
        ("duplicate_alias", "Duplicate source alias"),
        ("duplicate_order", "Duplicate adapter processing order"),
        ("unknown_catalog_service", "Unknown catalog service"),
        ("bad_url", "catalogs.json"),
        ("extra_field", "catalogs.json"),
        ("duplicate_organisation", "Duplicate organisation identity"),
    ],
)
def test_cross_references_and_shapes_are_rejected(tmp_path, damage, message):
    adapters, organisations, catalogs = (
        deepcopy(ADAPTERS),
        deepcopy(ORGANISATIONS),
        deepcopy(CATALOGS),
    )
    services = catalogs["services"]
    if damage == "missing_required":
        del adapters["adapters"]["alpha"]["defaults"]["languages"]
        del services["s:alpha:b"]["options"]["languages"]
    elif damage == "wrong_type":
        services["s:alpha:a"]["options"]["rate_limit"] = "fast"
    elif damage == "unknown_adapter":
        services["s:alpha:a"]["adapter"] = "gamma"
    elif damage == "unknown_publisher":
        services["s:alpha:a"]["publisher"] = "three"
    elif damage == "duplicate_alias":
        services["s:alpha:b"]["source_code"] = "a"
    elif damage == "duplicate_order":
        services["s:alpha:b"]["order"] = 1
    elif damage == "unknown_catalog_service":
        catalogs["catalogs"]["c:a"]["services"].append("s:none")
    elif damage == "bad_url":
        services["s:alpha:a"]["endpoint_url"] = "ftp://a.example"
    elif damage == "extra_field":
        services["s:alpha:a"]["scopes"] = {}
    elif damage == "duplicate_organisation":
        organisations["organisations"]["two"]["id"] = "urn:nordicintel:agent:one"
    with pytest.raises(ValueError, match=message):
        registry.load(write(tmp_path, adapters, organisations, catalogs))


def test_repository_configuration_loads():
    loaded = registry.load()
    assert loaded.adapters
    for adapter in loaded.adapters:
        for binding in loaded.bindings(adapter):
            assert binding.label and binding.endpoint_url.startswith("http")

"""JSON-schema loading and worker-document, provider and retrieval-result validation."""

import json
from functools import cache

from jsonschema import Draft202012Validator, FormatChecker

from apps.paths import SCHEMA
from retrieval._semantics import validate_cube

NAMES = {"tabular", "provider", "retrieval-result", "source-configuration"}


@cache
def schema(name: str) -> dict:
    """Load one of the bundled schemas without network resolution."""
    if name not in NAMES:
        raise ValueError(f"Unknown schema: {name}")
    return json.loads(
        SCHEMA.joinpath(name + ".schema.json").read_text(encoding="utf-8")
    )


@cache
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(schema(name), format_checker=FormatChecker())


def _check(name, document):
    errors = list(validator(name).iter_errors(document))
    if errors:
        raise ValueError("; ".join(f"{e.json_path}: {e.message}" for e in errors[:8]))


def validate_retrieval(document: dict, *, selection: dict) -> None:
    """Validate an observation fragment against the caller's full selection."""
    _check("retrieval-result", document)
    validate_cube(document, selection=selection)


def validate_provider(document: dict) -> None:
    """Validate the provider envelope, not adapter operational configuration."""
    _check("provider", document)


def validate_dataset(document: dict, *, adapter: str | None = None) -> None:
    """Validate a worker document's structure and cross-field relationships."""
    _check("tabular", document)
    validate_cube(document)
    provider = document["extension"]["nordicintel"]["provider_code"]
    allowed = {"nordicintel", provider}
    if adapter:
        allowed.add(adapter)

    def namespaces(item):
        if set(item.get("extension", {})) - allowed:
            raise ValueError(
                "Extension namespace does not belong to provider or adapter"
            )
        for links in item.get("link", {}).values():
            seen = set()
            for link in links:
                namespaces(link)
                key = (
                    link["href"],
                    link.get("extension", {}).get("nordicintel", {}).get("category_id"),
                )
                if key in seen:
                    raise ValueError("Duplicate resource in relation")
                seen.add(key)

    namespaces(document)
    for links in document.get("link", {}).values():
        for link in links:
            if "category_id" in link.get("extension", {}).get("nordicintel", {}):
                raise ValueError(
                    "Dataset resource cannot reference a dimension category"
                )
    for dimension in document["dimension"].values():
        namespaces(dimension)


def validate_metadata(document: dict, *, adapter: str | None = None) -> None:
    """Require full metadata and no observations or observation flags."""
    validate_dataset(document, adapter=adapter)
    if document["value"] != [] or "status" in document:
        raise ValueError("Harvest output must be metadata-only")

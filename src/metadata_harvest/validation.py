"""Offline shared schema and semantic validation; requires the harvest extra."""

import json
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

from metadata_harvest.models import Dataset
from metadata_harvest.semantics import validate_cube


@cache
def schema(name: str) -> dict:
    """Load a bundled authoritative schema without network resolution."""
    if name not in {
        "dataset-metadata",
        "legacy-dataset-metadata",
        "provider",
        "retrieval-result",
        "codelist",
        "harvest-contribution",
    }:
        raise ValueError(f"Unknown schema: {name}")
    return json.loads(
        files("metadata_harvest")
        .joinpath("schemas", name + ".schema.json")
        .read_text(encoding="utf-8")
    )


@cache
def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(schema(name), format_checker=FormatChecker())


def _check(name, document):
    errors = list(validator(name).iter_errors(document))
    if errors:
        raise ValueError("; ".join(f"{e.json_path}: {e.message}" for e in errors[:8]))


def validate_provider(document: dict) -> None:
    """Validate the provider envelope, not adapter operational configuration."""
    _check("provider", document)


def validate_dataset(document: dict, *, adapter: str | None = None) -> None:
    """Validate shared Dataset structure and cross-field relationships."""
    if isinstance(document, Dataset):
        return
    if document.get("@type") == "dcat:Dataset":
        Dataset.from_jsonld(document)
        return
    _check("legacy-dataset-metadata", document)
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
    if isinstance(document, Dataset) or document.get("@type") == "dcat:Dataset":
        return
    if document["value"] != [] or "status" in document:
        raise ValueError("Harvest output must be metadata-only")


def validate_retrieval(document: dict, *, selection: dict) -> None:
    """Validate an observation fragment against the caller's full selection."""
    _check("retrieval-result", document)
    validate_cube(document, selection=selection)

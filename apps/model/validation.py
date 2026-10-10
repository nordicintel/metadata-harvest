"""Repository-side generated contract and observation-result validation."""

import json
from functools import cache

from jsonschema import Draft202012Validator, FormatChecker

from apps.model.models import Dataset
from apps.paths import SCHEMAS
from retrieval._semantics import validate_cube


def validate_model(document):
    return document if isinstance(document, Dataset) else Dataset.from_jsonld(document)


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
        SCHEMAS.joinpath(name + ".schema.json").read_text(encoding="utf-8")
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

"""Offline shared schema and semantic validation; requires the harvest extra."""

from apps.model.models import Dataset
from apps.model.validation import _check, validate_model
from retrieval._semantics import validate_cube


def validate_provider(document: dict) -> None:
    """Validate the provider envelope, not adapter operational configuration."""
    _check("provider", document)


def validate_dataset(document: dict, *, adapter: str | None = None) -> None:
    """Validate shared Dataset structure and cross-field relationships."""
    if isinstance(document, Dataset):
        return
    if document.get("@type") == "dcat:Dataset":
        validate_model(document)
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

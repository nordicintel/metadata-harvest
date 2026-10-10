"""Ordered enrichment proposals; no external fetching or assignment rules yet."""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

from harvesting.processing import content_enrichment, semantic_enrichment


@dataclass(frozen=True)
class StageResult:
    """Field replacements use existing-parent JSON Pointers; errors are independent."""

    status: Literal["not_configured", "completed", "failed"]
    changes: dict = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _validate(before: dict, after: dict) -> None:
    from harvesting.validation import validate_metadata

    for key in ("id", "size"):
        if before.get(key) != after.get(key):
            raise ValueError(f"Enrichment cannot change {key}")
    for name, dimension in before.get("dimension", {}).items():
        if dimension.get("category") != after.get("dimension", {}).get(name, {}).get(
            "category"
        ):
            raise ValueError("Enrichment cannot change source categories")
    for dimension in before.get("dimension", {}):
        for name, extension in (
            before["dimension"][dimension].get("extension", {}).items()
        ):
            if (
                name != "nordicintel"
                and after["dimension"][dimension].get("extension", {}).get(name)
                != extension
            ):
                raise ValueError("Enrichment cannot change dimension source evidence")
    for name, extension in before.get("extension", {}).items():
        if name != "nordicintel" and after.get("extension", {}).get(name) != extension:
            raise ValueError("Enrichment cannot change source evidence")
    identity = ("provider_code", "dataset_code", "language")
    for key in identity:
        if before["extension"]["nordicintel"].get(key) != after["extension"][
            "nordicintel"
        ].get(key):
            raise ValueError("Enrichment cannot change source identity")
    from harvesting.registry import ADAPTERS

    adapter = next(
        (name for name in ADAPTERS if name in before.get("extension", {})), None
    )
    validate_metadata(after, adapter=adapter)


def process_dataset(dataset: dict, *, stages=None) -> tuple[dict, list[StageResult]]:
    """Apply valid stage proposals independently; retain input on failure."""
    current, outcomes = deepcopy(dataset), []
    for process in stages or (content_enrichment.process, semantic_enrichment.process):
        try:
            outcome = process(deepcopy(current))
            if outcome.status not in {"completed", "failed", "not_configured"}:
                raise ValueError("Invalid enrichment status")
            if outcome.changes and outcome.status != "completed":
                raise ValueError("Only completed stages can propose changes")
            if outcome.changes:
                candidate = deepcopy(current)
                for pointer, value in outcome.changes.items():
                    if not pointer.startswith("/"):
                        raise ValueError("Changes require JSON Pointers")
                    parts = [
                        p.replace("~1", "/").replace("~0", "~")
                        for p in pointer[1:].split("/")
                    ]
                    target = candidate
                    for part in parts[:-1]:
                        target = target[part]
                    target[parts[-1]] = deepcopy(value)
                _validate(current, candidate)
                current = candidate
            outcomes.append(outcome)
        except Exception as error:
            outcomes.append(
                StageResult("failed", errors=[f"{type(error).__name__}: {error}"])
            )
    return current, outcomes

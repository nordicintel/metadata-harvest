"""Identify accepted collection inputs and validate collection paths."""

from pathlib import Path, PurePosixPath

from apps.harvest.registry import ADAPTERS
from apps.publication.artifacts import manifest


def _collection_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (
        len(parts) < 3
        or parts[0] not in ADAPTERS
        or parts[1] not in {"metadata", "codelists"}
        or ".." in parts
        or "\\" in name
        or not name.endswith(".jsonl.gz")
    ):
        raise ValueError(f"Unsafe collection path: {name}")
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Unsafe collection path: {name}")
    return target


def inputs(data: Path, adapters: list[str]) -> dict:
    """Fingerprint only accepted collections and latest reports, never work/listings."""
    result = {}
    for adapter in adapters:
        for directory in ("metadata", "codelists", "reports"):
            root = data / adapter / directory
            for relative, digest in manifest(root).items():
                if (
                    directory == "reports" and relative.endswith(".json")
                ) or relative.endswith(".jsonl.gz"):
                    result[f"{adapter}/{directory}/{relative}"] = digest
    return result

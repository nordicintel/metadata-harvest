"""Source-neutral publication, checkpoint and artifact integrity helpers."""

import hashlib
from pathlib import Path

from metadata_harvest.registry import ADAPTERS


def manifest(root: Path) -> dict:
    """Fingerprint current content without the self-referencing result marker."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.name != "result.json":
            with path.open("rb") as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def checkpoint_path(root: str | Path) -> Path:
    return Path(root) / "work/current.json"


def publication_directory(root: Path, data: Path) -> str:
    """Limit Git publication to one implemented adapter's exact directory."""
    relative = data.resolve().relative_to(root.resolve()).as_posix()
    if relative not in {
        f"data/{name}" for name, spec in ADAPTERS.items() if spec.available
    }:
        raise ValueError("Publication requires a configured adapter data directory")
    return relative


def check_collection_sizes(root: str | Path, *, limit: int = 100 * 1024 * 1024) -> None:
    for path in Path(root).rglob("*"):
        if path.is_file() and path.stat().st_size >= limit:
            raise ValueError(
                f"Collection exceeds GitHub's file limit: {path} ({path.stat().st_size} bytes)"
            )


def report_status(*, pending: int, errors: list, interrupted: bool = False) -> str:
    return (
        "interrupted"
        if interrupted
        else "running"
        if pending
        else "completed_with_errors"
        if errors
        else "success"
    )

"""Harvest checkpoint paths and lifecycle status classification."""

from pathlib import Path


def checkpoint_path(root: str | Path) -> Path:
    return Path(root) / "work/current.json"


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

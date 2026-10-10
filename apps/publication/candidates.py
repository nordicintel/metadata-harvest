"""Verify candidate integrity and freshness before explicit promotion."""

import shutil
import subprocess
from pathlib import Path

from apps.catalog.inputs import _collection_path, inputs
from apps.paths import CONFIG
from apps.publication.archives import CATALOG_FILES, read
from apps.publication.artifacts import manifest


def verify_candidate(data: Path, output: Path, *, config_dir: Path = CONFIG) -> dict:
    """Check all paths and fingerprints before any catalog mutation."""
    data, output = Path(data), Path(output)
    record = read(output / "result.json")
    if "dependencies" not in record:
        raise ValueError(
            "Stale catalog candidate; build with current application dependencies"
        )
    if "dependencies" in record:
        from apps.catalog.dependencies import dependencies

        if dependencies(data, config_dir=config_dir) != record["dependencies"]:
            raise ValueError("Stale catalog dependencies; rebuild")
    if (
        inputs(data, record["adapters"]) != record["inputs"]
        or manifest(data / "catalog") != record["catalog_baseline"]
    ):
        raise ValueError("Stale catalog candidate; rebuild from published files")
    if manifest(output) != record["candidate"]:
        raise ValueError("Candidate integrity mismatch")
    if set(record["candidate"]) != CATALOG_FILES:
        raise ValueError("Unexpected catalog candidate files")
    return record


def promote(data: Path, output: Path, *, config_dir: Path = CONFIG) -> dict:
    """Promote assets locally and remove only verified legacy catalog collections."""
    data, output = Path(data), Path(output)
    record = verify_candidate(data, output, config_dir=config_dir)
    legacy = [
        _collection_path(data / "catalog", name)
        for name in record["catalog_baseline"]
        if name not in CATALOG_FILES
    ]
    for name in record["candidate"]:
        target = (data / "catalog" / name).resolve()
        if not target.is_relative_to((data / "catalog").resolve()):
            raise ValueError("Unsafe catalog candidate path")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(output / name, temporary)
        temporary.replace(target)
    for path in legacy:
        path.unlink()
    return read(output / "reports/latest.json")


def verify_published(data: Path, adapters: list[str]) -> None:
    """CLI inputs must be committed accepted results, not local worker changes."""
    paths = [
        str(data / adapter / kind)
        for adapter in adapters
        for kind in ("metadata", "codelists", "reports")
    ]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise ValueError(
            "Catalog requires committed adapter results; publish those deliberately first"
        )
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve()
    tracked = set(
        subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *paths],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\0")
    )
    for name in inputs(data, adapters):
        source = (data / name).resolve()
        if (
            not source.is_relative_to(root)
            or source.relative_to(root).as_posix() not in tracked
        ):
            raise ValueError("Catalog input is not committed in HEAD: " + str(source))

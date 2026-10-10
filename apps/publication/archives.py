"""Streaming catalog archive I/O and deterministic Zstandard compression."""

import hashlib
import io
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import zstandard

ASSETS = ("catalog.jsonl.zst", "codelists.jsonl.zst")


LEGACY_ASSETS = ("catalog.7z", "codelists.7z")


CATALOG_FILES = {*ASSETS, "state/current.json", "reports/latest.json"}


def previous_asset(state: dict, kind: str) -> str:
    """Resolve an accepted asset from its manifest, including pre-Zstandard output."""
    for name in (kind + ".jsonl.zst", kind + ".7z"):
        if name in state.get("assets", {}):
            return name
    raise ValueError(f"Missing previous {kind} asset")


def sevenzip() -> str:
    """Find the native 7-Zip command on Linux, macOS or Windows."""
    for name in ("7zz", "7z"):
        if executable := shutil.which(name):
            return executable
    if os.name == "nt":
        executable = (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe"
        )
        if executable.is_file():
            return str(executable)
    raise FileNotFoundError("Reading legacy .7z catalogs requires 7-Zip (7zz or 7z)")


@contextmanager
def open_catalog(path: Path):
    """Read decompressed bytes as a bounded-memory stream; no extracted file required."""
    path = Path(path)
    if path.suffix == ".zst":
        with (
            path.open("rb") as raw,
            zstandard.ZstdDecompressor().stream_reader(raw) as reader,
            io.BufferedReader(reader) as buffered,
        ):
            yield buffered
    elif path.suffix == ".7z":
        with subprocess.Popen(
            [sevenzip(), "e", "-so", str(path), path.stem + ".jsonl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                yield process.stdout
                if process.stdout.read(1):
                    # A caller may deliberately stop early; do not drain a large archive.
                    process.terminate()
                elif process.wait() != 0:
                    raise ValueError(f"Cannot decode legacy catalog: {path}")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.stdout.close()
    else:
        raise ValueError(f"Unsupported catalog compression: {path.suffix}")


def _extract(archive: Path, target: Path, member: str) -> None:
    expected = (
        archive.name.removesuffix(".zst")
        if archive.suffix == ".zst"
        else archive.stem + ".jsonl"
    )
    if member != expected:
        raise ValueError("Unexpected catalog member")
    with open_catalog(archive) as source, target.open("wb") as stream:
        shutil.copyfileobj(source, stream, length=1024 * 1024)


def _compress(source: Path, archive: Path) -> None:
    """Create a deterministic Zstandard level 6 stream with a content checksum."""
    temporary = archive.with_name(archive.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with source.open("rb") as incoming, temporary.open("wb") as raw:
            with zstandard.ZstdCompressor(level=6, write_checksum=True).stream_writer(
                raw
            ) as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)

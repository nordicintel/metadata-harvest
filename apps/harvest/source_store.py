"""Per-dataset source files with a manifest of identifiers, locations and dates.

A harvest persists only what the source returned: one envelope per dataset and
language, text companions when a response is not JSON, and shared files for
responses several datasets depend on. ``manifest.json`` indexes every entry with
its files, the source's own last-update signal (stored verbatim) and the retrieval
time, which is what refresh logic compares. Parsing into StatDCAT happens later,
outside the harvest, from these files.
"""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from apps.harvest.storage import write
from apps.publication.artifacts import manifest as directory_manifest

MANIFEST = "manifest.json"
SHARED = "_shared"
VERSION = 1
_NAME_BUDGET = 65


class Rejected(ValueError):
    """The body cannot be stored: empty, missing or not serialisable."""


def source_key(provider_code: str, dataset_code: str, language: str) -> str:
    return f"{provider_code}/{dataset_code}/{language}"


def split_key(key: str) -> tuple[str, str, str]:
    provider, rest = key.split("/", 1)
    code, language = rest.rsplit("/", 1)
    return provider, code, language


def source_filename(
    dataset_code: str, language: str, part: str | None = None, suffix: str = ".json"
) -> str:
    quoted = quote(dataset_code, safe="")
    if len(quoted) > _NAME_BUDGET:
        digest = hashlib.sha256(dataset_code.encode("utf-8")).hexdigest()[:12]
        quoted = f"{quoted[:_NAME_BUDGET]}__{digest}"
    return f"{quoted}.{language}" + (f".{part}" if part else suffix)


def harvest_manifest(data: Path) -> dict:
    """The harvest's own files: everything except the parse-owned ``metadata/``."""
    return {
        path: digest
        for path, digest in directory_manifest(data).items()
        if not path.startswith("metadata/")
    }


def _check(name: str, body) -> None:
    if body is None or body == "" or body == [] or body == {}:
        raise Rejected(f"Empty response for {name}")
    try:
        json.dumps(body, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise Rejected(f"Unserialisable response for {name}: {error}") from error


def _read_text(path: Path) -> str:
    """Read exactly what was written, without newline translation."""
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if not path.exists() or path.read_bytes() != temporary.read_bytes():
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class SourceStore:
    """Envelopes and shared files under ``data/<adapter>/source``.

    Changes are held in memory until ``save()``, which writes changed files only,
    writes the manifest last and then deletes files the manifest no longer lists.
    """

    def __init__(self, root: str | Path, adapter: str):
        self.root = Path(root)
        self.adapter = adapter
        path = self.root / MANIFEST
        if path.exists():
            self.manifest = json.loads(path.read_text(encoding="utf-8"))
            if self.manifest.get("adapter") != adapter:
                raise ValueError(f"Manifest belongs to {self.manifest.get('adapter')}")
        else:
            self.manifest = {
                "adapter": adapter,
                "version": VERSION,
                "entries": {},
                "shared": {},
            }
        self._pending: dict[str, object] = {}  # relative path -> json body or text

    def entries(self) -> dict[str, dict]:
        return self.manifest["entries"]

    def _read(self, relative: str):
        if relative in self._pending:
            return self._pending[relative]
        path = self.root / relative
        if not path.exists():
            return None
        if relative.endswith(".json"):
            return json.loads(path.read_text(encoding="utf-8"))
        return _read_text(path)

    def read_file(self, relative: str):
        return self._read(relative)

    def get(self, key: str) -> dict | None:
        entry = self.manifest["entries"].get(key)
        return None if entry is None else self._read(entry["files"][0])

    def put(
        self,
        key: str,
        *,
        responses: dict,
        updated,
        retrieved_at: str,
        derived: dict | None = None,
        files: dict[str, str] | None = None,
    ) -> None:
        if not responses:
            raise Rejected(f"No responses for {key}")
        for name, body in responses.items():
            _check(f"{key} {name}", body)
        for name, text in (files or {}).items():
            if not isinstance(text, str) or not text:
                raise Rejected(f"Empty companion file {name} for {key}")
        _check(key, {"derived": derived or {}, "updated": updated})
        provider, code, language = split_key(key)
        previous = self.get(key)
        if (
            previous is not None
            and previous["responses"] == responses
            and previous["derived"] == (derived or {})
            and previous["updated"] == updated
            and all(
                self._read(relative) == text
                for relative, text in self._companions(key, files)
            )
        ):
            retrieved_at = previous["retrieved_at"]
        envelope = {
            "key": key,
            "identifier": code,
            "language": language,
            "updated": updated,
            "retrieved_at": retrieved_at,
            "responses": responses,
            "derived": derived or {},
        }
        main = f"{provider}/{source_filename(code, language)}"
        paths = [main]
        self._pending[main] = envelope
        for relative, text in self._companions(key, files):
            self._pending[relative] = text
            paths.append(relative)
        self.manifest["entries"][key] = {
            "files": paths,
            "updated": updated,
            "retrieved_at": retrieved_at,
        }

    @staticmethod
    def _companions(key: str, files: dict[str, str] | None) -> list[tuple[str, str]]:
        provider, code, language = split_key(key)
        return [
            (f"{provider}/{source_filename(code, language, part=part)}", text)
            for part, text in sorted((files or {}).items())
        ]

    def remove(self, key: str) -> None:
        entry = self.manifest["entries"].pop(key, None)
        for relative in entry["files"] if entry else ():
            self._pending.pop(relative, None)

    def get_shared(self, provider_code: str, name: str):
        return self._read(f"{provider_code}/{SHARED}/{name}")

    def put_shared(self, provider_code: str, name: str, body, *, retrieved_at: str):
        _check(f"{provider_code}/{name}", body)
        relative = f"{provider_code}/{SHARED}/{name}"
        self._pending[relative] = body
        self.manifest["shared"][relative] = {"retrieved_at": retrieved_at}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for relative in sorted(self._pending):
            body = self._pending[relative]
            path = self.root / relative
            if relative.endswith(".json"):
                write(path, body)
                if json.loads(path.read_text(encoding="utf-8")) != body:
                    raise ValueError(f"Source file failed verification: {relative}")
            else:
                _write_text(path, body)
                if _read_text(path) != body:
                    raise ValueError(f"Source file failed verification: {relative}")
        manifest = {
            "adapter": self.adapter,
            "version": VERSION,
            "entries": {
                key: {**entry, "files": sorted(entry["files"])}
                for key, entry in sorted(self.manifest["entries"].items())
            },
            "shared": dict(sorted(self.manifest["shared"].items())),
        }
        write(self.root / MANIFEST, manifest)
        self.manifest = manifest
        self._pending.clear()
        referenced = {MANIFEST, *self.manifest["shared"]}
        for entry in self.manifest["entries"].values():
            referenced.update(entry["files"])
        for path in sorted(self.root.rglob("*")):
            if (
                path.is_file()
                and path.relative_to(self.root).as_posix() not in referenced
            ):
                path.unlink()
        for directory in sorted(self.root.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

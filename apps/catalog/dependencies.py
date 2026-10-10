"""Fingerprint explicit configuration, source code and accepted portal inputs."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from apps.paths import CONFIG, ROOT


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def configuration(config_dir: Path = CONFIG) -> tuple[dict, dict]:
    path = Path(config_dir) / "catalog.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if type(config.get("include_scb_pxweb_v1", False)) is not bool:
        raise ValueError("include_scb_pxweb_v1 must be boolean")
    config.setdefault("include_scb_pxweb_v1", False)
    overrides = Path(config_dir) / "catalog-matches.json"
    matches = (
        json.loads(overrides.read_text(encoding="utf-8")) if overrides.exists() else {}
    )
    return config, matches


def dependencies(data: Path, *, config_dir: Path = CONFIG) -> dict:
    result = {}
    for name in (
        "catalog.toml",
        "catalog-matches.json",
        "adapters.json",
        "organisations.json",
        "catalogs.json",
        "profile.json",
    ):
        path = Path(config_dir) / name
        result["config:" + name] = _hash(path) if path.exists() else None
    for name in ("oppnadata.jsonl.gz", "state.json"):
        path = data / "data_eu" / name
        result["portal:" + name] = _hash(path) if path.exists() else None
    for directory in ("model", "catalog", "publication"):
        for path in sorted((ROOT / "apps" / directory).glob("*.py")):
            result["code:" + path.relative_to(ROOT).as_posix()] = _hash(path)
    for name in (
        "configuration.py",
        "paths.py",
        "harvest/contributions.py",
        "harvest/sources.py",
        "harvest/accepted.py",
        "harvest/registry.py",
        "migrations/profile_reader.py",
        "portal/snapshot.py",
    ):
        result["code:apps/" + name] = _hash(ROOT / "apps" / name)
    return result

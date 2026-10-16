"""Fingerprint explicit configuration, source code and accepted portal inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from apps.paths import CONFIG, ROOT
from apps.settings import load as load_settings


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def configuration(config_dir: Path = CONFIG) -> tuple[dict, dict]:
    config = load_settings(config_dir)["catalog"]
    overrides = Path(config_dir) / "catalog-matches.json"
    matches = (
        json.loads(overrides.read_text(encoding="utf-8")) if overrides.exists() else {}
    )
    return config, matches


def dependencies(data: Path, *, config_dir: Path = CONFIG) -> dict:
    # Only the catalog section matters; other settings must not force rebuilds.
    catalog = json.dumps(configuration(config_dir)[0], sort_keys=True)
    result = {
        "config:settings.toml[catalog]": hashlib.sha256(catalog.encode()).hexdigest()
    }
    for name in (
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
        "settings.py",
        "parsing/contributions.py",
        "harvest/sources.py",
        "parsing/accepted.py",
        "harvest/registry.py",
        "migrations/profile_reader.py",
        "portal/snapshot.py",
    ):
        result["code:apps/" + name] = _hash(ROOT / "apps" / name)
    return result

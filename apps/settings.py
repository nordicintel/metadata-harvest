"""Load repository-wide operational settings from config/settings.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from apps.paths import CONFIG

DEFAULTS = {
    "scheduling": {"paused": False},
    "catalog": {"include_scb_pxweb_v1": False},
}


def load(config_dir: Path = CONFIG) -> dict:
    """Validate known boolean settings; a missing file or key uses its default."""
    path = Path(config_dir) / "settings.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if set(raw) - set(DEFAULTS):
        raise ValueError(
            f"Unknown settings sections: {sorted(set(raw) - set(DEFAULTS))}"
        )
    result = {}
    for section, defaults in DEFAULTS.items():
        values = raw.get(section, {})
        if set(values) - set(defaults):
            raise ValueError(
                f"Unknown {section} settings: {sorted(set(values) - set(defaults))}"
            )
        for key, value in values.items():
            if type(value) is not bool:
                raise ValueError(f"{section}.{key} must be boolean")
        result[section] = {**defaults, **values}
    return result

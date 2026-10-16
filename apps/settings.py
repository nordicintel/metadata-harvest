"""Load repository-wide operational settings from config/settings.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from apps.paths import CONFIG

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "OFF")

# Each default fixes the key's type; string keys additionally list allowed values.
DEFAULTS = {
    "scheduling": {"paused": False},
    "logging": {"level": "INFO"},
}
CHOICES = {("logging", "level"): LOG_LEVELS}


def load(config_dir: Path = CONFIG) -> dict:
    """Validate known settings; a missing file or key uses its default."""
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
            expected = type(defaults[key])
            if type(value) is not expected:
                raise ValueError(f"{section}.{key} must be {expected.__name__}")
            choices = CHOICES.get((section, key))
            if choices and value not in choices:
                raise ValueError(f"{section}.{key} must be one of {list(choices)}")
        result[section] = {**defaults, **values}
    return result

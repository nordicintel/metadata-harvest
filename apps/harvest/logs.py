"""Shared logging setup inherited by every metadata adapter and the catalog build.

Adapter modules log through ``logging.getLogger("<adapter>_logger")`` and other
repository modules through ``logging.getLogger(__name__)``; the names below cover
all of them, so one handler and one level apply to the whole repository. The level
comes from ``logging.level`` in config/settings.toml and ``OFF`` silences them.
"""

from __future__ import annotations

import logging
import sys

from apps.harvest.metadata_adapters import ADAPTERS
from apps.settings import load as load_settings

# Repository modules log under their package name; every metadata adapter instead
# logs under its own deterministic "<adapter>_logger" name so the adapters are
# distinguishable in run output regardless of which module emitted the line.
NAMESPACES = ("apps", "retrieval", *(f"{name}_logger" for name in ADAPTERS))


def adapter_logger(adapter: str) -> logging.Logger:
    """The shared logger for one metadata adapter, for example ``sdb_logger``."""
    if adapter not in ADAPTERS:
        raise ValueError(f"Unknown adapter: {adapter}")
    return logging.getLogger(f"{adapter}_logger")


FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def configure(level: str | None = None, *, stream=None) -> None:
    """Install one readable stderr handler on the repository loggers.

    Calling it again replaces the previous handler, so tests and long-lived
    processes can change the level without duplicating output lines.
    """
    level = (level or load_settings()["logging"]["level"]).upper()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    handler.set_name("metadata-harvest")
    for name in NAMESPACES:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            if existing.get_name() == handler.get_name():
                logger.removeHandler(existing)
        logger.propagate = False
        if level == "OFF":
            logger.disabled = True
            continue
        logger.disabled = False
        logger.setLevel(level)
        logger.addHandler(handler)

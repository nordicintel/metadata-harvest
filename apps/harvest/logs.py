"""Shared logging setup inherited by every metadata adapter.

Adapters log through ``logging.getLogger(__name__)``; the names below are the
ancestors of all of them, so one handler and one level cover the whole set. The
level comes from ``logging.level`` in config/settings.toml and ``OFF`` silences
the adapters entirely.
"""

from __future__ import annotations

import logging
import sys

from apps.settings import load as load_settings

ADAPTER_LOGGERS = ("apps.harvest.metadata_adapters", "retrieval.data_adapters")
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def configure(level: str | None = None, *, stream=None) -> None:
    """Install one readable stderr handler on the adapter loggers.

    Calling it again replaces the previous handler, so tests and long-lived
    processes can change the level without duplicating output lines.
    """
    level = (level or load_settings()["logging"]["level"]).upper()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    handler.set_name("metadata-adapters")
    for name in ADAPTER_LOGGERS:
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

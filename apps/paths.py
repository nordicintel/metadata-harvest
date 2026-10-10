"""Stable repository resource locations, independent of the working directory."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
SCHEMAS = ROOT / "schemas"

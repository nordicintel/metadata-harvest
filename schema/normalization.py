"""Stable identifiers and loss-aware text and temporal normalization."""

from __future__ import annotations

import calendar
import re
from datetime import date
from urllib.parse import quote


def stable_id(kind: str, *parts: str) -> str:
    return "urn:nordicintel:" + kind + ":" + ":".join(quote(p, safe="") for p in parts)


def language_text(value, language):
    return {language: [value]} if isinstance(value, str) else {language: value}


def _period(value, *, end=False):
    if not isinstance(value, str):
        return None
    if value.startswith("0000"):
        return None
    if re.fullmatch(r"\d{4}", value):
        return value + ("-12-31" if end else "-01-01")
    month = re.fullmatch(r"(\d{4})(?:M|-)(\d{2})", value)
    quarter = re.fullmatch(r"(\d{4})K([1-4])", value)
    if quarter:
        year, q = map(int, quarter.groups())
        m = q * 3 if end else (q - 1) * 3 + 1
        return date(year, m, calendar.monthrange(year, m)[1] if end else 1).isoformat()
    if month:
        year, m = map(int, month.groups())
        try:
            return date(
                year, m, calendar.monthrange(year, m)[1] if end else 1
            ).isoformat()
        except ValueError:
            return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


RESOLUTIONS = {
    "annual": "P1Y",
    "quarterly": "P3M",
    "monthly": "P1M",
    "weekly": "P1W",
    "daily": "P1D",
}

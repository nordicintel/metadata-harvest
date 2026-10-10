"""Parse UKÄ's indicator catalogue and ``/api/totals/{id}`` responses."""

import html
import re
from dataclasses import dataclass
from typing import Any

from retrieval._uka_export import API_ROOT

CATALOGUE_URL = f"{API_ROOT}/indicators"
DISCONTINUED = "uppdateras inte längre"
"""UKÄ's wording for an indicator that is no longer updated ("OBS! Statistiken
uppdateras inte längre", "(UPPDATERAS INTE LÄNGRE)"), matched case-insensitively."""

_TAG = re.compile(r"<[^>]*>")
_HREF = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_URL = re.compile(r"^[Hh][Tt][Tt][Pp][Ss]?://[^\s/?#]+[^\s]*$")


@dataclass(frozen=True)
class Entry:
    """An indicator listed in the catalogue."""

    id: int
    name: str
    group: str
    most_recent_url: str | None


@dataclass(frozen=True)
class Indicator:
    """An indicator's ``/api/totals/{id}`` metadata."""

    id: int
    name: str
    description: str | None
    measurement: str | None
    current_year: str | None
    periods: tuple[str, ...]
    """Periods with a national total, in UKÄ's (chronological) order."""


def totals_url(indicator_id: int | str) -> str:
    return f"{API_ROOT}/totals/{indicator_id}"


def parse_catalogue(body: Any) -> list[Entry]:
    """Return the catalogue's indicators, grouped as listed.

    Raises:
        ValueError: The body is not the expected ``{group: [indicator, …]}`` object
            or an indicator is listed twice.
    """
    if not isinstance(body, dict):
        raise ValueError(f"unexpected UKÄ catalogue {str(body)[:200]!r}")
    entries: list[Entry] = []
    for group, items in body.items():
        if not isinstance(items, list):
            raise ValueError(f"catalogue group {group!r} is not a list")
        for item in items:
            entries.append(
                Entry(
                    id=int(item["id"]),
                    name=str(item["name"]).strip(),
                    group=str(item.get("indicator_group") or group).strip(),
                    most_recent_url=item.get("most_recent_url"),
                )
            )
    ids = [entry.id for entry in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("catalogue lists an indicator more than once")
    return entries


def parse_totals(body: Any, indicator_id: int) -> Indicator:
    """Return an indicator's metadata from its unfiltered totals response.

    Raises:
        LookupError: UKÄ answers with an error object (such as "Indicator has no
            datasets.").
        ValueError: The response lacks the indicator object or yearly totals.
    """
    if not isinstance(body, dict):
        raise ValueError(f"unexpected totals response {str(body)[:200]!r}")
    if isinstance(error := body.get("error"), str):
        raise LookupError(f"UKÄ indicator {indicator_id}: {error}")
    indicator = body.get("indicator")
    yearly = body.get("yearly_totals")
    if not isinstance(indicator, dict) or not isinstance(yearly, dict):
        raise ValueError(f"indicator {indicator_id} lacks indicator or yearly totals")
    periods = [str(item["year"]).strip() for item in yearly.get("totals") or []]
    if len(set(periods)) != len(periods):
        raise ValueError(f"indicator {indicator_id} lists a period twice")
    return Indicator(
        id=indicator_id,
        name=str(indicator.get("name") or "").strip(),
        description=_text(indicator.get("description")),
        measurement=_text(indicator.get("measurement")),
        current_year=_text(indicator.get("current_year")),
        periods=tuple(periods),
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def plain_text(description: str | None) -> str | None:
    """Return an HTML description as plain text (link texts kept, space collapsed)."""
    if description is None:
        return None
    text = " ".join(html.unescape(_TAG.sub(" ", description)).split())
    return text or None


def documentation_url(description: str | None) -> str | None:
    """Return the first absolute link in an HTML description (UKÄ's "Beskrivning
    av statistiken" or "Läs mer här" page), if any."""
    for href in _HREF.findall(description or ""):
        href = html.unescape(href).strip()
        if _URL.match(href):
            return href
    return None


def is_discontinued(indicator: Indicator) -> bool:
    """Whether UKÄ states that the indicator is no longer updated."""
    texts = (indicator.description or "", indicator.measurement or "")
    return any(DISCONTINUED in text.casefold() for text in texts)

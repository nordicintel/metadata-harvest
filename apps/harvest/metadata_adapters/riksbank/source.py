"""Riksbank catalogue responses parsed into per-dataset harvest records.

A record is a JSON-serializable dict naming its ``api`` and ``dataset_code`` with
everything the mapping needs besides the one probe request:

- ``swea``: one per SWEA group that has series (``swea_group_{groupId}``), with the
  group, its chain from the root group and its ``/Series`` rows;
- ``mpd``: one per monetary-policy forecast series (``mpd_{series_id}``), with its
  ``/forecasts/series_ids`` row and the ``/forecasts/policy_rounds`` list;
- ``swestr``: the single SWESTR dataset.

Listing parsers isolate bad rows: a malformed or duplicated row is reported as a
problem, the datasets it would change are quarantined (not rebuilt), and the
listing is marked incomplete so that it cannot prove any removal.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger("riksbank_logger")

_ROUND = re.compile(r"\d{4}:\d+")
_SERIES_ID = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class Listing:
    """Records discovered for one API.

    Attributes:
        records: Buildable dataset records, in source order.
        problems: ``(resource, error)`` for malformed or duplicated rows.
        quarantined: Dataset codes not rebuilt because of a bad row.
        complete: Whether the listing can prove removals.
    """

    records: list[dict] = field(default_factory=list)
    problems: list[tuple[str, Exception]] = field(default_factory=list)
    quarantined: set[str] = field(default_factory=set)
    complete: bool = True

    def problem(self, resource: str, error: Exception, code: str | None = None):
        self.problems.append((resource, error))
        self.complete = False
        if code:
            self.quarantined.add(code)
        logger.warning(
            "Catalogue problem in %s: %s; quarantined (not rebuilt): %s; listing "
            "marked incomplete",
            resource,
            error,
            code or "no dataset",
        )


def swea_code(group_id: int) -> str:
    return f"swea_group_{group_id}"


def mpd_code(series_id: str) -> str:
    return f"mpd_{series_id}"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_date(value: Any) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def parse_groups(tree: Any, listing: Listing) -> dict[int, dict]:
    """Every group of the ``/swea/v1/Groups`` tree keyed by ``groupId``.

    Each value has ``groupId``, ``name``, ``description`` and ``chain``: the
    ``{id, label}`` path from the root group to the group itself.

    Raises:
        ValueError: The response is not a group tree.
    """
    if not isinstance(tree, Mapping) or not isinstance(tree.get("groupId"), int):
        raise ValueError(f"unexpected SWEA group tree {str(tree)[:200]!r}")
    groups: dict[int, dict] = {}
    duplicates: set[int] = set()

    def visit(node: Any, chain: list[dict], where: str) -> None:
        group_id = node.get("groupId") if isinstance(node, Mapping) else None
        name = _text(node.get("name")) if isinstance(node, Mapping) else ""
        if not isinstance(group_id, int) or not name:
            listing.problem(
                f"swea/{where}", ValueError(f"Malformed group {str(node)[:200]}")
            )
            return
        path = [*chain, {"id": str(group_id), "label": name}]
        if group_id in groups:
            duplicates.add(group_id)
        groups[group_id] = {
            "groupId": group_id,
            "name": name,
            "description": _text(node.get("description")),
            "chain": path,
        }
        children = node.get("childGroups") or []
        if not isinstance(children, list):
            listing.problem(
                f"swea/{where}", ValueError(f"Malformed childGroups of {group_id}")
            )
            return
        for number, child in enumerate(children):
            visit(child, path, f"{where}.{number}")

    visit(tree, [], "groups")
    for group_id in sorted(duplicates):
        listing.problem(
            f"en/{swea_code(group_id)}",
            ValueError(f"group {group_id} is listed more than once"),
            swea_code(group_id),
        )
    logger.debug("SWEA /Groups: %d groups in the tree", len(groups))
    return groups


_SERIES_TEXT = ("source", "shortDescription", "midDescription", "longDescription")


def _series_problem(row: Any) -> str | None:
    if not isinstance(row, Mapping):
        return "not an object"
    if not isinstance(row.get("seriesId"), str) or not _SERIES_ID.fullmatch(
        row["seriesId"]
    ):
        return "missing or invalid seriesId"
    if not isinstance(row.get("groupId"), int):
        return "missing groupId"
    if not all(
        _is_date(row.get(k)) for k in ("observationMinDate", "observationMaxDate")
    ):
        return "missing observation dates"
    if not isinstance(row.get("seriesClosed"), bool):
        return "missing seriesClosed"
    if not (_text(row.get("midDescription")) or _text(row.get("shortDescription"))):
        return "missing description"
    if any(
        row.get(k) is not None and not isinstance(row[k], str) for k in _SERIES_TEXT
    ):
        return "non-text description"
    return None


def swea_listing(groups_body: Any, series_body: Any) -> Listing:
    """Group records from the ``/Groups`` and ``/Series`` responses.

    Raises:
        ValueError: Either response has the wrong shape, or no group has series.
    """
    listing = Listing()
    groups = parse_groups(groups_body, listing)
    if not isinstance(series_body, list):
        raise ValueError(f"unexpected SWEA series list {str(series_body)[:200]!r}")
    by_group: dict[int, list[dict]] = {}
    seen: dict[str, list[int]] = {}
    for number, row in enumerate(series_body):
        if reason := _series_problem(row):
            group = row.get("groupId") if isinstance(row, Mapping) else None
            code = swea_code(group) if isinstance(group, int) else None
            resource = f"en/{code}" if code else f"swea/series.{number}"
            listing.problem(
                resource, ValueError(f"Malformed series row: {reason}"), code
            )
            continue
        by_group.setdefault(row["groupId"], []).append(dict(row))
        seen.setdefault(row["seriesId"], []).append(row["groupId"])
    for series_id, group_ids in sorted(seen.items()):
        if len(group_ids) > 1:
            for group_id in sorted(set(group_ids)):
                listing.problem(
                    f"en/{swea_code(group_id)}",
                    ValueError(f"series {series_id} is listed more than once"),
                    swea_code(group_id),
                )
    logger.debug(
        "SWEA /Series: %d rows in %d groups with series (%d valid series ids)",
        len(series_body),
        len(by_group),
        len(seen),
    )
    for group_id, rows in by_group.items():
        code = swea_code(group_id)
        if group_id not in groups:
            listing.problem(
                f"en/{code}",
                ValueError(f"series reference group {group_id}, absent from /Groups"),
                code,
            )
            continue
        if code in listing.quarantined:
            continue
        listing.records.append(
            {
                "api": "swea",
                "dataset_code": code,
                "group": groups[group_id],
                "series": rows,
            }
        )
    if not listing.records and not listing.quarantined:
        logger.error("The SWEA catalogue lists no series")
        raise ValueError("the SWEA catalogue lists no series")
    return listing


def policy_rounds(body: Any) -> list[str]:
    """Policy round names, oldest first (``2015:1`` … ``2026:3``).

    Raises:
        ValueError: The response is not a non-empty list of round names.
    """
    rounds = body.get("data") if isinstance(body, Mapping) else None
    if (
        not isinstance(rounds, list)
        or not rounds
        or not all(isinstance(r, str) and _ROUND.fullmatch(r) for r in rounds)
        or len(set(rounds)) != len(rounds)
    ):
        raise ValueError(f"unexpected policy round list {str(body)[:200]!r}")
    ordered = sorted(rounds, key=lambda r: tuple(int(p) for p in r.split(":")))
    logger.debug(
        "Forecast policy rounds: %d, %s to %s", len(ordered), ordered[0], ordered[-1]
    )
    return ordered


def _mpd_problem(row: Any) -> str | None:
    if not isinstance(row, Mapping):
        return "not an object"
    if not isinstance(row.get("series_id"), str) or not _SERIES_ID.fullmatch(
        row["series_id"]
    ):
        return "missing or invalid series_id"
    meta = row.get("metadata")
    if not isinstance(meta, Mapping):
        return "missing metadata"
    if not _text(meta.get("description")):
        return "missing description"
    if meta.get("start_date") is not None and not _is_date(meta.get("start_date")):
        return "invalid start_date"
    return None


def mpd_listing(series_body: Any, rounds_body: Any) -> Listing:
    """Forecast series records from ``series_ids`` and ``policy_rounds``.

    Raises:
        ValueError: Either response has the wrong shape, or no series is listed.
    """
    rounds = policy_rounds(rounds_body)
    rows = series_body.get("data") if isinstance(series_body, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError(f"unexpected forecast series list {str(series_body)[:200]!r}")
    listing = Listing()
    found: dict[str, list[dict]] = {}
    for number, row in enumerate(rows):
        if reason := _mpd_problem(row):
            series_id = row.get("series_id") if isinstance(row, Mapping) else None
            code = mpd_code(series_id) if isinstance(series_id, str) else None
            resource = f"en/{code}" if code else f"mpd/series_ids.{number}"
            listing.problem(
                resource, ValueError(f"Malformed series row: {reason}"), code
            )
            continue
        found.setdefault(row["series_id"], []).append(dict(row))
    for series_id, matches in found.items():
        code = mpd_code(series_id)
        if len(matches) > 1:
            listing.problem(f"en/{code}", ValueError("Listed more than once"), code)
        elif code not in listing.quarantined:
            listing.records.append(
                {
                    "api": "mpd",
                    "dataset_code": code,
                    "series": matches[0],
                    "policy_rounds": rounds,
                }
            )
    logger.debug(
        "Forecast series_ids: %d rows, %d distinct series", len(rows), len(found)
    )
    if not listing.records and not listing.quarantined:
        logger.error("The forecast API lists no series")
        raise ValueError("the forecast API lists no series")
    return listing


def swestr_listing(latest: Any) -> Listing:
    """The SWESTR dataset with its ``/swestr/v1/latest/SWESTR`` response.

    There is no catalogue: the latest fixing is both discovery and probe.

    Raises:
        ValueError: The response is empty or not a fixing.
    """
    if not isinstance(latest, Mapping) or not _is_date(latest.get("date")):
        raise ValueError(f"unexpected latest SWESTR response {str(latest)[:200]!r}")
    return Listing(
        records=[{"api": "swestr", "dataset_code": "swestr", "latest": dict(latest)}]
    )

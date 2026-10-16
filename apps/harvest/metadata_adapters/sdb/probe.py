"""Reconcile SDB's variable listing with its result endpoint.

SDB's data API does not always match the metadata API: the injury subjects filter
and report the ``yttreorsak`` variable as ``diagnos``, and monthly causes of death
list only recent years although older years have data. Result rows start with the
earliest year, so years before the listing are probed from the first row's year, and
years after it up to the current year. Deep pagination is avoided: SDB answers
HTTP 500 slowly for very high page numbers.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apps.harvest.metadata_adapters.sdb.client import (
    NotFoundError,
    SdbClient,
    Unreachable,
)
from retrieval.data_adapters.sdb import result_field

logger = logging.getLogger("sdb_logger")


@dataclass(frozen=True)
class ResultProbe:
    """What a sample of the result endpoint revealed about a subject.

    Attributes:
        result_dimensions: Variable names mapped to differing result field names.
        added_periods: ``ar`` codes with observations that the variable listing lacks.
    """

    result_dimensions: dict[str, str] = field(default_factory=dict)
    added_periods: list[str] = field(default_factory=list)


def _result_dimensions(
    names: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """Listed variables mapped to differing result field names.

    Raises:
        RuntimeError: Result fields cannot be matched to the listed variables.
    """
    fields = {result_field(key) for key in rows[0] if key != "varde"}
    unmatched_names = [name for name in names if name not in fields]
    unmatched_fields = sorted(fields - set(names))
    if len(unmatched_names) != len(unmatched_fields) or len(unmatched_names) > 1:
        raise RuntimeError(
            f"Cannot match SDB variables {unmatched_names} to result fields "
            f"{unmatched_fields}."
        )
    return dict(zip(unmatched_names, unmatched_fields, strict=True))


def _year_plan(
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
    ar_field: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    today_year: int,
) -> tuple[list[int], set[str]]:
    """Candidate years outside the listed range, and years seen in the first row."""
    if "ar" not in categories:
        return [], set()
    listed = {str(category["id"]) for category in categories["ar"]}
    observed = {
        str(value)
        for row in rows
        for key, value in row.items()
        if result_field(key) == ar_field
    }
    codes = listed | observed
    if not listed or not all(code.isdigit() for code in codes):
        return [], observed  # periods such as "1987-1989" are not probed
    first_listed = min(int(code) for code in listed)
    last_listed = max(int(code) for code in listed)
    candidates = [
        *range(min(int(code) for code in codes), first_listed),
        *range(last_listed + 1, today_year + 1),
    ]
    return candidates, observed


async def fetch_probe(
    client: SdbClient,
    language: str,
    subject: str,
    variables: Sequence[Mapping[str, Any]],
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Sample the result endpoint and probe unlisted years; return the raw bodies.

    The first row request is also the subject's endpoint health check. The result
    is ``{"language", "first", "years", "candidates"}``: ``years`` maps each
    requested candidate year to its body, or ``None`` for HTTP 404; years already
    seen in the first row are not requested. Reconciliation with the listing
    happens in :func:`reconcile`; when result fields cannot be matched no years
    are probed and ``reconcile`` reports the mismatch.

    Raises:
        Unreachable: The result endpoint fails or serves no observations.
    """
    names = [str(variable["namn"]) for variable in variables]
    logger.debug(
        "%s/%s: probing the result endpoint (first row) against %d listed variables",
        language,
        subject,
        len(names),
    )
    try:
        first = await client.get(language, subject, "resultat", params={"per_sida": 1})
    except NotFoundError as error:
        logger.debug("%s/%s: result endpoint answered 404", language, subject)
        raise Unreachable(f"{subject}: result endpoint serves no rows") from error
    rows = list(first["data"])
    if not rows:
        logger.debug("%s/%s: result endpoint serves no rows", language, subject)
        raise Unreachable(f"{subject}: result endpoint serves no rows")
    raw = {"language": language, "first": first, "years": {}, "candidates": []}
    try:
        result_dimensions = _result_dimensions(names, rows)
    except RuntimeError as error:
        logger.warning("%s/%s: %s; unlisted years not probed", language, subject, error)
        return raw
    ar_field = result_dimensions.get("ar", "ar")
    candidates, observed = _year_plan(
        categories, ar_field, rows, today_year=datetime.now(UTC).year
    )
    raw["candidates"] = candidates
    logger.debug(
        "%s/%s: probing %d candidate years outside the listed range",
        language,
        subject,
        len(candidates),
    )
    for year in candidates:
        if str(year) in observed:
            continue
        try:
            raw["years"][str(year)] = await client.get(
                language,
                subject,
                "resultat",
                ar_field,
                str(year),
                params={"per_sida": 1},
            )
        except NotFoundError:
            logger.debug("%s/%s: no observations for %s", language, subject, year)
            raw["years"][str(year)] = None
    return raw


def reconcile(
    raw: Mapping[str, Any],
    variables: Sequence[Mapping[str, Any]],
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ResultProbe:
    """Reconcile stored probe bodies with the listing, without any request.

    Raises:
        RuntimeError: Result fields cannot be matched to the listed variables.
    """
    names = [str(variable["namn"]) for variable in variables]
    rows = list(raw["first"]["data"])
    result_dimensions = _result_dimensions(names, rows)
    ar_field = result_dimensions.get("ar", "ar")
    _, observed = _year_plan(categories, ar_field, rows, today_year=0)
    added = [
        str(year)
        for year in raw.get("candidates", [])
        if str(year) in observed or raw["years"].get(str(year)) is not None
    ]
    return ResultProbe(result_dimensions, added)


async def probe_results(
    client: SdbClient,
    language: str,
    subject: str,
    variables: Sequence[Mapping[str, Any]],
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ResultProbe:
    """Fetch and reconcile in one step (typed boundary and live checks)."""
    return reconcile(
        await fetch_probe(client, language, subject, variables, categories),
        variables,
        categories,
    )

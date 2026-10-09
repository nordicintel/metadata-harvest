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

from metadata_harvest.adapters.sdb.client import NotFoundError, SdbClient, Unreachable
from metadata_harvest.adapters.sdb.retrieval import result_field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResultProbe:
    """What a sample of the result endpoint revealed about a subject.

    Attributes:
        result_dimensions: Variable names mapped to differing result field names.
        added_periods: ``ar`` codes with observations that the variable listing lacks.
    """

    result_dimensions: dict[str, str] = field(default_factory=dict)
    added_periods: list[str] = field(default_factory=list)


async def probe_results(
    client: SdbClient,
    language: str,
    subject: str,
    variables: Sequence[Mapping[str, Any]],
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ResultProbe:
    """Sample a subject's first result row and reconcile it with the listing.

    The first row request is also the subject's endpoint health check.

    Raises:
        Unreachable: The result endpoint fails or serves no observations.
        RuntimeError: Result fields cannot be matched to the listed variables.
    """
    names = [str(variable["namn"]) for variable in variables]
    try:
        first = await client.get(language, subject, "resultat", params={"per_sida": 1})
    except NotFoundError as error:
        raise Unreachable(f"{subject}: result endpoint serves no rows") from error
    rows = list(first["data"])
    if not rows:
        raise Unreachable(f"{subject}: result endpoint serves no rows")

    fields = {result_field(key) for key in rows[0] if key != "varde"}
    unmatched_names = [name for name in names if name not in fields]
    unmatched_fields = sorted(fields - set(names))
    if len(unmatched_names) != len(unmatched_fields) or len(unmatched_names) > 1:
        raise RuntimeError(
            f"Cannot match SDB variables {unmatched_names} to result fields "
            f"{unmatched_fields}."
        )
    result_dimensions = dict(zip(unmatched_names, unmatched_fields, strict=True))
    added_periods = await _missing_years(
        client, language, subject, categories, result_dimensions, rows
    )
    return ResultProbe(result_dimensions, added_periods)


async def _missing_years(
    client: SdbClient,
    language: str,
    subject: str,
    categories: Mapping[str, Sequence[Mapping[str, Any]]],
    result_dimensions: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    if "ar" not in categories:
        return []
    ar_field = result_dimensions.get("ar", "ar")
    listed = {str(category["id"]) for category in categories["ar"]}
    observed = {
        str(value)
        for row in rows
        for key, value in row.items()
        if result_field(key) == ar_field
    }
    codes = listed | observed
    if not listed or not all(code.isdigit() for code in codes):
        return []  # periods such as "1987-1989" are not probed
    first_listed = min(int(code) for code in listed)
    last_listed = max(int(code) for code in listed)
    candidates = [
        *range(min(int(code) for code in codes), first_listed),
        *range(last_listed + 1, datetime.now(UTC).year + 1),
    ]
    added = []
    for year in candidates:
        if str(year) not in observed:
            try:
                await client.get(
                    language,
                    subject,
                    "resultat",
                    ar_field,
                    str(year),
                    params={"per_sida": 1},
                )
            except NotFoundError:
                continue
        added.append(str(year))
    if added:
        logger.info(
            "%s: %d years have observations but are not listed", subject, len(added)
        )
    return added

"""Kolada v3 transport: listings, batched change windows and single-KPI rescans.

Observation payloads are consumed in memory, never stored in output/state/logs.
"""

import asyncio
import logging
from time import monotonic

import httpx

from apps.harvest.metadata_adapters.kolada.metadata import BASE_URL, SCOPES

logger = logging.getLogger("kolada_logger")

BATCH = 25
PER_PAGE = 5000


def chunks(items, size=BATCH):
    items = list(items)
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def transient(error):
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in {429, 500, 502, 503, 504}
    )


def flatten(rows, scope):
    """One record per value cell: kpi, period, entity, gender, deleted."""
    records = []
    for row in rows:
        if (
            not isinstance(row.get("kpi"), str)
            or not isinstance(row.get("period"), int)
            or not isinstance(row.get(scope), str)
            or not isinstance(row.get("values"), list)
        ):
            raise ValueError("Malformed Kolada data row")
        for cell in row["values"]:
            if cell.get("gender") not in {"M", "K", "T"}:
                raise ValueError("Unknown gender category")
            records.append(
                {
                    "kpi": row["kpi"],
                    "period": row["period"],
                    "entity": row[scope],
                    "gender": cell["gender"],
                    "deleted": bool(cell.get("isdeleted")),
                }
            )
    return records


class BudgetExpired(TimeoutError):
    """End the current run without recording a source failure."""


class KoladaClient:
    """Shared limiter over at most four concurrent requests."""

    def __init__(self, provider: dict, *, http=None):
        self.base = provider.get("base_url", BASE_URL).rstrip("/")
        self.delay = provider.get("rate_limit", 0.225)
        self.timeout = provider.get("timeout", 150)
        self.http = http or httpx.AsyncClient(timeout=self.timeout)
        self.owned = http is None
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(4)
        self.next_request = 0.0
        self.requests = 0
        self.deadline = None
        logger.info(
            "Kolada client: base=%s rate_limit=%ss timeout=%ss",
            self.base,
            self.delay,
            self.timeout,
        )

    async def close(self):
        logger.info("Kolada client closing after %d HTTP requests", self.requests)
        if self.owned:
            await self.http.aclose()

    async def get(self, path: str, params=None, *, attempts=3):
        remaining = None if self.deadline is None else self.deadline - monotonic()
        if remaining is not None and remaining <= 0:
            raise BudgetExpired("Request budget exhausted")
        try:
            async with asyncio.timeout(remaining):
                return await self._get(path, params, attempts=attempts)
        except TimeoutError as error:
            raise BudgetExpired("Request budget exhausted") from error

    async def _get(self, path: str, params=None, *, attempts=3):
        for attempt in range(attempts):
            try:
                async with self.slots:
                    async with self.lock:
                        await asyncio.sleep(max(0, self.next_request - monotonic()))
                        self.next_request = monotonic() + self.delay
                    self.requests += 1
                    response = await self.http.get(
                        f"{self.base}/{path.lstrip('/')}",
                        params=params,
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("values"), list
                    ):
                        raise ValueError(f"Malformed Kolada envelope: {path}")
                    if any(not isinstance(row, dict) for row in payload["values"]):
                        raise ValueError(f"Malformed Kolada rows: {path}")
                    return payload
            except httpx.HTTPError as error:
                if not transient(error) or attempt == attempts - 1:
                    raise
                retry = (
                    error.response.headers.get("Retry-After", "")
                    if isinstance(error, httpx.HTTPStatusError)
                    else ""
                )
                wait = float(retry) if retry.isdigit() else 2**attempt
                logger.warning(
                    "Transient failure on GET %s (%s); retrying in %ss",
                    path,
                    type(error).__name__,
                    wait,
                )
                await asyncio.sleep(wait)

    async def rows(self, path, params=None):
        """All rows of a paginated resource."""
        params = {"per_page": PER_PAGE, **(params or {})}
        rows = []
        for page in range(1, 5001):
            payload = await self.get(path, {**params, "page": page})
            rows.extend(payload["values"])
            if not payload.get("next_url"):
                return rows
        raise ValueError(f"Pagination exceeded 5000 pages: {path}")

    async def listing(self, name):
        rows = await self.rows(name)
        logger.debug("Listing %s: %d rows", name, len(rows))
        return rows

    def changes_path(self, scope, batch, years):
        """Municipality windows select areas; OU windows select KPIs."""
        selector = "municipality" if scope == "municipality" else "kpi"
        return (
            f"{SCOPES[scope]}/{selector}/{','.join(batch)}"
            f"/year/{','.join(map(str, years))}"
        )

    async def changed_records(self, scope, ids, years, from_date):
        """Every cell added or deleted since from_date, across all batched windows.

        Raises after the whole scan when any window failed; successful windows
        never advance anything on their own, so a failure costs only a retry.
        """
        paths = [
            self.changes_path(scope, batch, window)
            for batch in chunks(ids)
            for window in chunks(years)
        ]
        logger.info(
            "Scanning %s changes since %s: %d windows", scope, from_date, len(paths)
        )
        results = await asyncio.gather(
            *(self.rows(p, {"from_date": from_date}) for p in paths),
            return_exceptions=True,
        )
        records, failures = [], []
        for path, result in zip(paths, results, strict=True):
            if isinstance(result, BudgetExpired):
                raise result
            if isinstance(result, Exception):
                failures.append(f"{path}: {type(result).__name__}: {result}")
                continue
            records.extend(flatten(result, scope))
        if failures:
            raise ValueError(
                f"{len(failures)} of {len(paths)} change windows failed: "
                + "; ".join(failures[:3])
            )
        logger.info("%s changes since %s: %d cells", scope, from_date, len(records))
        return records

    async def scope_records(self, scope, kpi, years):
        """Every current cell of one KPI, for a clean rebuild after deletions."""
        rows = []
        for window in chunks(years):
            rows.extend(
                await self.rows(
                    f"{SCOPES[scope]}/kpi/{kpi}/year/{','.join(map(str, window))}"
                )
            )
        records = flatten(rows, scope)
        if any(r["kpi"] != kpi or r["period"] not in years for r in records):
            raise ValueError("Scope response is outside requested KPI/years")
        return records

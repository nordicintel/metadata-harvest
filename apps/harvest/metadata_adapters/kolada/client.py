"""Kolada v3 harvesting transport and compact change/coverage probes.

Observation payloads are consumed in memory, never stored in output/state/logs.
"""

import asyncio
import logging
from datetime import date, timedelta
from time import monotonic

import httpx

from apps.harvest.metadata_adapters.kolada.metadata import BASE_URL, SCOPES

logger = logging.getLogger("kolada_logger")


def chunks(items, size):
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def transient(error):
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and error.response.status_code in {429, 500, 502, 503, 504}
    )


class BudgetExpired(TimeoutError):
    """End the current worker wave without recording a source failure."""


class KoladaClient:
    """One limiter across all requests, with injectable shared HTTP connections."""

    def __init__(self, provider: dict, *, http=None):
        self.base = provider.get("base_url", BASE_URL).rstrip("/")
        self.delay = provider.get("rate_limit", 0.225)
        self.timeout = provider.get("timeout", 130)
        self.group_ids = provider.get("group_ids", [])
        self.http = http or httpx.AsyncClient(timeout=self.timeout)
        self.owned = http is None
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(4)
        self.next_request = 0.0
        self.requests = 0
        self.deadline = None
        logger.info(
            "Kolada client: base=%s rate_limit=%ss timeout=%ss group_ids=%d "
            "(max 4 concurrent requests)",
            self.base,
            self.delay,
            self.timeout,
            len(self.group_ids),
        )

    async def close(self):
        logger.info("Kolada client closing after %d HTTP requests", self.requests)
        if self.owned:
            await self.http.aclose()

    async def get(self, path: str, params=None, *, attempts=3):
        remaining = None if self.deadline is None else self.deadline - monotonic()
        if remaining is not None and remaining <= 0:
            logger.warning("Request budget exhausted before GET %s", path)
            raise BudgetExpired("Worker request budget exhausted")
        try:
            async with asyncio.timeout(remaining):
                return await self._get(path, params, attempts=attempts)
        except TimeoutError as error:
            logger.warning("Request budget exhausted during GET %s", path)
            raise BudgetExpired("Worker request budget exhausted") from error

    async def _get(self, path: str, params=None, *, attempts=3):
        for attempt in range(attempts):
            try:
                async with self.slots:
                    async with self.lock:
                        await asyncio.sleep(max(0, self.next_request - monotonic()))
                        self.next_request = monotonic() + self.delay
                    self.requests += 1
                    logger.debug(
                        "GET %s params=%s (request %d, attempt %d/%d)",
                        path,
                        params,
                        self.requests,
                        attempt + 1,
                        attempts,
                    )
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
                    if not payload["values"] and payload.get("next_url"):
                        raise ValueError(f"Empty nonfinal page: {path}")
                    return payload
            except (httpx.HTTPError, httpx.TransportError) as error:
                if not transient(error) or attempt == attempts - 1:
                    if transient(error):
                        logger.warning(
                            "GET %s failed after %d attempts: %s",
                            path,
                            attempts,
                            type(error).__name__,
                        )
                    else:
                        logger.debug("GET %s failed without retry: %s", path, error)
                    raise
                retry = (
                    error.response.headers.get("Retry-After", "")
                    if isinstance(error, httpx.HTTPStatusError)
                    else ""
                )
                logger.warning(
                    "Transient failure on GET %s (%s); backing off %ss before "
                    "attempt %d/%d",
                    path,
                    type(error).__name__
                    if not isinstance(error, httpx.HTTPStatusError)
                    else error.response.status_code,
                    float(retry) if retry.isdigit() else 2**attempt,
                    attempt + 2,
                    attempts,
                )
                await asyncio.sleep(float(retry) if retry.isdigit() else 2**attempt)

    async def pages(self, path, params=None):
        params = {"per_page": 5000, **(params or {})}
        previous = None
        for page in range(1, 5001):
            payload = await self.get(path, {**params, "page": page})
            rows = payload["values"]
            if rows and rows == previous:
                raise ValueError(f"Repeated page: {path}, page {page}")
            yield rows
            if not payload.get("next_url"):
                return
            previous = rows
        raise ValueError(f"Pagination exceeded 5000 pages: {path}")

    async def listing(self, name):
        rows = []
        async for page in self.pages(name):
            rows.extend(page)
        logger.debug("Listing %s: %d rows", name, len(rows))
        return rows

    async def kpi(self, identifier):
        try:
            rows = await self.listing(f"kpi/{identifier}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                logger.debug("KPI %s returned 404", identifier)
                return None
            raise
        if not rows:
            logger.debug("KPI %s returned no rows", identifier)
            return None
        if len(rows) != 1 or rows[0].get("id") != identifier:
            raise ValueError("KPI identity mismatch")
        return rows[0]

    def query(self, scope, ids, years, *, areas=None):
        path = f"{SCOPES[scope]}/kpi/{','.join(ids)}"
        if areas:
            path += f"/{'ou' if scope == 'ou' else 'municipality'}/{','.join(areas)}"
        return path + f"/year/{','.join(map(str, years))}"

    async def _changed_window(self, scope, ids, years, cutoff, *, areas=None):
        remaining = list(ids)
        found = set()
        try:
            while remaining:
                payload = await self.get(
                    self.query(scope, remaining, years, areas=areas),
                    {
                        "per_page": 1000,
                        "page": 1,
                        **({"from_date": cutoff} if cutoff else {}),
                    },
                    attempts=1,
                )
                hits = {r["kpi"] for r in payload["values"]}
                if not hits <= set(remaining) or any(
                    r.get("period") not in years for r in payload["values"]
                ):
                    raise ValueError("Change response is outside requested scope")
                found |= hits
                remaining = sorted(set(remaining) - hits)
                if not payload.get("next_url"):
                    return found
                # Drop positively identified KPIs and ask again, rather than downloading their values.
                if not hits:
                    raise ValueError("Change probe did not advance")
        except httpx.HTTPError as error:
            if not transient(error):
                raise
            split = ids if len(ids) >= len(years) else years
            if len(split) > 1:
                logger.warning(
                    "Change probe for %s (%d KPIs, %d years) failed transiently "
                    "(%s); splitting the %s in two",
                    scope,
                    len(ids),
                    len(years),
                    type(error).__name__,
                    "KPIs" if split is ids else "years",
                )
                mid = len(split) // 2
                found = set()
                for part in (split[:mid], split[mid:]):
                    found |= await self._changed_window(
                        scope,
                        part if split is ids else ids,
                        years if split is ids else part,
                        cutoff,
                        areas=areas,
                    )
                return found
            logger.warning(
                "Change probe for %s/%s year %s cannot be split further; "
                "retrying with a minimal page",
                scope,
                ",".join(ids),
                ",".join(map(str, years)),
            )
            payload = await self.get(
                self.query(scope, ids, years, areas=areas),
                {"per_page": 1, **({"from_date": cutoff} if cutoff else {})},
            )
            if any(
                r.get("kpi") not in ids or r.get("period") not in years
                for r in payload["values"]
            ):
                raise ValueError(
                    "Change response is outside requested scope"
                ) from error
            return {r["kpi"] for r in payload["values"]}
        return found

    async def changed(self, scope, ids, years, cutoff):
        found = set()
        areas = [None] + (
            list(chunks(self.group_ids, 25)) if scope == "municipality" else []
        )
        for period in reversed(list(chunks(years, 25))):
            for area in areas:
                remaining = sorted(set(ids) - found)
                if not remaining:
                    logger.debug(
                        "Change probe %s: all %d KPIs already flagged; "
                        "skipping remaining windows",
                        scope,
                        len(ids),
                    )
                    return found
                for batch in chunks(remaining, 25):
                    found |= await self._changed_window(
                        scope, batch, period, cutoff, areas=area
                    )
        return found

    async def modified(self, scope, identifier, years, *, earliest, today):
        """Native positive-day/empty-next-day boundary, only for affected KPIs."""
        upper = today + timedelta(days=1)
        logger.debug(
            "Dating last change of %s/%s: searching %s..%s",
            scope,
            identifier,
            earliest,
            upper,
        )
        if await self.changed(scope, [identifier], years, upper.isoformat()):
            raise ValueError("Future cutoff returns data; cannot establish change date")
        if not await self.changed(scope, [identifier], years, earliest.isoformat()):
            logger.debug(
                "%s/%s: nothing changed since %s; modification date unknown",
                scope,
                identifier,
                earliest,
            )
            return None
        lo, hi = earliest.toordinal(), upper.toordinal()
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if await self.changed(
                scope, [identifier], years, date.fromordinal(mid).isoformat()
            ):
                lo = mid
            else:
                hi = mid
        day = date.fromordinal(lo)
        if not await self.changed(
            scope, [identifier], years, day.isoformat()
        ) or await self.changed(
            scope, [identifier], years, (day + timedelta(days=1)).isoformat()
        ):
            raise ValueError("Source changed during date probe")
        logger.debug("%s/%s: last changed %s", scope, identifier, day)
        return day.isoformat()

    async def coverage(self, scope, identifier, years, references):
        """Stream all current rows into small category sets, including null/suppressed cells."""
        periods, genders, entities = set(), set(), set()
        if scope == "municipality":
            ids = sorted(set(references["municipality"]) | set(self.group_ids))
            areas = list(chunks(ids, 25))
        else:
            areas = [None]
        logger.debug(
            "Collecting %s/%s coverage: %d years, %d area batches",
            scope,
            identifier,
            len(years),
            len(areas),
        )
        for window in chunks(years, 25):
            for area in areas:
                async for rows in self.pages(
                    self.query(scope, [identifier], window, areas=area)
                ):
                    for row in rows:
                        if (
                            row.get("kpi") != identifier
                            or row.get("period") not in window
                            or (area and row.get(scope) not in area)
                        ):
                            raise ValueError(
                                "Coverage response is outside requested scope"
                            )
                        if not isinstance(row.get(scope), str) or not isinstance(
                            row.get("values"), list
                        ):
                            raise ValueError("Malformed coverage row")
                        for value in row["values"]:
                            if value.get("isdeleted"):
                                continue
                            if value.get("gender") not in {"M", "K", "T"}:
                                raise ValueError("Unknown gender category")
                            periods.add(row["period"])
                            entities.add(row[scope])
                            genders.add(value["gender"])
        result = {"years": sorted(periods), "genders": sorted(genders)}
        logger.debug(
            "%s/%s coverage: %d years, %d entities, genders %s",
            scope,
            identifier,
            len(periods),
            len(entities),
            sorted(genders),
        )
        if scope == "ou":
            result["ou_ids"] = sorted(entities)
        else:
            groups = set(references["municipality_groups"]) | set(self.group_ids)
            result.update(
                municipality_ids=sorted(entities - groups),
                municipality_group_ids=sorted(entities & groups),
            )
        return result

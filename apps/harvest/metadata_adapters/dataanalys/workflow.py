"""Weekly analysverktyg rebuild with independent area and indicator failures.

The site publishes no change signal that covers the dimension categories, and a full
harvest is about 170 requests, so every run refetches every indicator; identical
responses keep their stored bytes. Discovery reads the chart list (area tables and
the charts' ``Matt_id`` filters) and the dashboard list (web pages); both are stored
as shared source files, as are each area's catalogue and column probe. Per
indicator, one query on the values table gives the dimension rows, is its endpoint
check and is stored as its envelope together with the catalogue row, columns, area
and link it was issued for. Every listed indicator is stored, whether or not the
current mapping can describe it. An indicator is published only while that endpoint
works and its entry was verified within ``max_age_days``; otherwise it is withheld
in state while its source files remain. Only a complete chart list in an unbounded
run proves an indicator's removal. Documents are built later by ``parse``.
"""

import asyncio
import logging
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.dataanalys.api import (
    CATALOGUE_OPTIONAL,
    CATALOGUE_REQUIRED,
    CATALOGUE_ROW_LIMIT,
    CHART_COLUMNS,
    CHART_LIST_URL,
    CUBE_ROW_LIMIT,
    DASHBOARD_COLUMNS,
    DASHBOARD_LIST_URL,
    DATA_URL,
    PAGE_SIZE,
    TRENDER_COLUMNS,
    catalogue_body,
    cube_body,
    list_query,
    missing_columns,
    probe_body,
    result_rows,
)
from apps.harvest.metadata_adapters.dataanalys.client import (
    DataanalysClient,
    RequestRejected,
    Unreachable,
)
from apps.harvest.metadata_adapters.dataanalys.mapping import MAPPING_VERSION
from apps.harvest.metadata_adapters.dataanalys.parse import document_from, link_record
from apps.harvest.metadata_adapters.dataanalys.source import (
    Area,
    Catalogue,
    Discovery,
    add_charts,
    add_dashboards,
    check_page,
    listing_complete,
    overview_dashboard,
    parse_catalogue,
    parse_cube,
    resolve_link,
)
from apps.harvest.metadata_adapters.dataanalys.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    load_state,
    save,
)
from apps.harvest.source_store import (
    Rejected,
    SourceStore,
    harvest_manifest,
    source_key,
    split_key,
)
from apps.harvest.storage import read, write
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest

logger = logging.getLogger("dataanalys_logger")

ADAPTER = "dataanalys"
LANGUAGES = ("sv",)
COUNTS = (
    "rebuilt",
    "unchanged",
    "out_of_scope",
    "failed",
    "withheld",
    "restored",
    "removed",
)
MAX_PAGES = 50
"""Stop paging a list after this many pages (5,000 rows): a runaway guard."""


def _sample(items, limit: int = 10) -> str:
    """A short, readable list of ``items`` for a log line."""
    items = list(items)
    shown = ", ".join(str(item) for item in items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def area_of(key: str) -> str:
    """The area code of a ``<language>/<AREA>|<indicator>`` key."""
    return key.split("/", 1)[1].split("|", 1)[0]


def indicator_of(key: str) -> str:
    return key.split("/", 1)[1].split("|", 1)[1]


def stored_key(key: str) -> str:
    """The source-store key of a ``<language>/<AREA>|<indicator>`` state key."""
    language, code = key.split("/", 1)
    return source_key(PROVIDER_CODE, code, language)


def state_key(key: str) -> str:
    _, code, language = split_key(key)
    return f"{language}/{code}"


def _allowed_path(relative: str) -> bool:
    return relative in {STATE, REPORT} or relative.startswith("source/")


async def _list(
    client, url: str, columns, resource: str, pages: list | None = None
) -> tuple[list, int, Exception | None]:
    """Retain understood pages when a later page fails; stop only this scope.

    ``pages`` receives each understood page body as received.
    """
    rows: list = []
    count = 0
    for page in range(MAX_PAGES):
        try:
            body = await client.get_json(url, {"q": list_query(columns, page)})
            page_count, result = check_page(body, resource)
            if page and page_count != count:
                raise ValueError(
                    f"{resource} listing count changed from {count} to {page_count}"
                )
            count = page_count
        except Exception as error:
            logger.warning(
                "%s listing stopped at page %d with %d of %d rows kept (%s: %s)",
                resource,
                page,
                len(rows),
                count,
                type(error).__name__,
                error,
            )
            return rows, count, error
        rows += result
        if pages is not None:
            pages.append(body)
        logger.debug(
            "%s listing page %d: %d rows (%d of %d)",
            resource,
            page,
            len(result),
            len(rows),
            count,
        )
        if len(result) < PAGE_SIZE or len(rows) >= count:
            break
    else:
        logger.warning(
            "%s listing reached the %d page guard with %d of %d rows",
            resource,
            MAX_PAGES,
            len(rows),
            count,
        )
    return rows, count, None


async def discover(client, bodies: dict | None = None) -> Discovery:
    """Read the chart and dashboard lists.

    Page failures are recorded independently; cancellation still propagates.
    ``bodies`` receives ``{"charts": [...pages], "dashboards": [...pages]}``.
    """
    discovery = Discovery()
    bodies = {} if bodies is None else bodies
    bodies.update(charts=[], dashboards=[])
    logger.info("Discovery: reading the chart and dashboard lists")
    charts, discovery.charts_count, error = await _list(
        client, CHART_LIST_URL, CHART_COLUMNS, "chart", bodies["charts"]
    )
    discovery.charts_listed = len(charts)
    discovery.charts_complete = error is None and listing_complete(
        charts, discovery.charts_count
    )
    if error is not None:
        discovery.problems.append(("charts", error))
    add_charts(discovery, charts)
    dashboards, discovery.dashboards_count, error = await _list(
        client, DASHBOARD_LIST_URL, DASHBOARD_COLUMNS, "dashboard", bodies["dashboards"]
    )
    discovery.dashboards_listed = len(dashboards)
    discovery.dashboards_complete = error is None and listing_complete(
        dashboards, discovery.dashboards_count
    )
    if error is not None:
        discovery.problems.append(("dashboards", error))
    add_dashboards(discovery, dashboards)
    logger.info(
        "Discovery: charts %d of %d listed (complete=%s), dashboards %d of %d "
        "listed (complete=%s), %d usable dashboards",
        discovery.charts_listed,
        discovery.charts_count,
        discovery.charts_complete,
        discovery.dashboards_listed,
        discovery.dashboards_count,
        discovery.dashboards_complete,
        len(discovery.dashboards),
    )
    logger.info(
        "Discovery: %d complete areas (%s), %d incomplete, %d unsupported "
        "datasources ignored, %d unreadable listing rows",
        len(discovery.areas),
        _sample(sorted(discovery.areas)),
        len(discovery.incomplete),
        len(discovery.unsupported),
        len(discovery.problems),
    )
    for code, reason in sorted(discovery.incomplete.items()):
        logger.warning("Discovery: area %s cannot be harvested: %s", code, reason)
    if discovery.unsupported:
        logger.debug("Discovery: unsupported datasources %s", discovery.unsupported)
    return discovery


class UnsupportedLayout(ValueError):
    """An area table lacks a column the adapter's model requires."""


class _Harvester:
    """Catalogue, column probe and document builds for areas of one discovery."""

    def __init__(self, client, discovery: Discovery):
        self.client, self.discovery = client, discovery
        self.columns_cache: dict[str, dict[str, str]] = {}
        self.catalogue_bodies: dict[str, dict] = {}
        """Per area: the catalogue query body and the columns it was asked for."""
        self.columns_bodies: dict[str, dict] = {}
        """Per area: the successful column probe body and the columns it chose."""

    async def catalogue(self, area: Area) -> Catalogue:
        """The area's catalogue; optional columns the table lacks are dropped once.

        Raises:
            Unreachable: The catalogue query did not answer.
            UnsupportedLayout: A required catalogue column is missing.
            ValueError: The catalogue may be truncated.
        """
        columns = [*CATALOGUE_REQUIRED, *CATALOGUE_OPTIONAL]
        for attempt in (1, 2):
            logger.debug(
                "Area %s: catalogue query %d on %s with %d columns",
                area.code,
                attempt,
                area.catalogue_dataset,
                len(columns),
            )
            try:
                body = await self.client.post_json(
                    DATA_URL, catalogue_body(area.catalogue_id, columns)
                )
            except RequestRejected as error:
                missing = missing_columns(error.text) if error.status == 400 else None
                if not missing or attempt == 2:
                    raise
                if required := [m for m in missing if m not in CATALOGUE_OPTIONAL]:
                    raise UnsupportedLayout(
                        f"{area.catalogue_dataset} lacks catalogue columns {required}"
                    ) from error
                columns = [c for c in columns if c not in missing]
                logger.info(
                    "Area %s: %s lacks optional catalogue columns %s; "
                    "querying again without them",
                    area.code,
                    area.catalogue_dataset,
                    missing,
                )
                continue
            rows = result_rows(body)
            if len(rows) >= CATALOGUE_ROW_LIMIT:
                raise ValueError(
                    f"{area.catalogue_dataset}: {len(rows)} catalogue rows reach the "
                    f"row limit {CATALOGUE_ROW_LIMIT}; the catalogue may be truncated"
                )
            catalogue = parse_catalogue(rows, columns)
            self.catalogue_bodies[area.code] = {"columns": list(columns), "body": body}
            logger.info(
                "Area %s: catalogue lists %d indicators (%d rows, %d duplicated "
                "codes, %d malformed rows)",
                area.code,
                len(catalogue.rows),
                len(rows),
                len(catalogue.duplicates),
                len(catalogue.problems),
            )
            return catalogue
        raise AssertionError("unreachable")

    async def columns(self, area: Area) -> dict[str, str]:
        """Logical ``Trender`` column to source column, learnt by a one-row probe.

        Raises:
            Unreachable: The probe did not answer.
            UnsupportedLayout: A required logical column has no candidate.
        """
        if area.code in self.columns_cache:
            logger.debug("Area %s: reusing probed Trender columns", area.code)
            return self.columns_cache[area.code]
        choice = dict.fromkeys(TRENDER_COLUMNS, 0)
        longest = max(len(candidates) for candidates, _ in TRENDER_COLUMNS.values())
        for _ in range(longest + 1):
            current = {
                name: candidates[choice[name]]
                for name, (candidates, _) in TRENDER_COLUMNS.items()
                if choice[name] < len(candidates)
            }
            try:
                body = await self.client.post_json(
                    DATA_URL, probe_body(area.values_id, list(current.values()))
                )
            except RequestRejected as error:
                missing = missing_columns(error.text) if error.status == 400 else None
                if not missing:
                    raise
                logical = {column: name for name, column in current.items()}
                if unknown := [m for m in missing if m not in logical]:
                    raise ValueError(
                        f"{area.values_dataset}: unexpected missing columns {unknown}"
                    ) from error
                for column in missing:
                    name = logical[column]
                    choice[name] += 1
                    candidates, required = TRENDER_COLUMNS[name]
                    if required and choice[name] >= len(candidates):
                        raise UnsupportedLayout(
                            f"{area.values_dataset} has none of {list(candidates)}"
                        ) from error
                    logger.debug(
                        "Area %s: %s has no column %s for %s; %s",
                        area.code,
                        area.values_dataset,
                        column,
                        name,
                        "trying the next alias"
                        if choice[name] < len(candidates)
                        else "optional, dropped",
                    )
                continue
            result_rows(body)
            self.columns_cache[area.code] = current
            self.columns_bodies[area.code] = {"columns": dict(current), "body": body}
            logger.info(
                "Area %s: probed %s columns %s",
                area.code,
                area.values_dataset,
                ", ".join(f"{name}={column}" for name, column in current.items()),
            )
            return current
        raise ValueError(f"{area.values_dataset}: columns could not be resolved")

    async def fetch(
        self, area: Area, code: str, row: dict, columns: dict[str, str]
    ) -> tuple[dict, dict, bool]:
        """One indicator's source responses, derived context and whether the
        current mapping can describe it (it has Riket or Region rows).

        Raises:
            Unreachable: The values query (the endpoint check) did not answer.
            ValueError: The dimension rows reach the row limit (truncated).
        """
        logger.debug("%s|%s: querying dimension categories", area.code, code)
        body = await self.client.post_json(
            DATA_URL, cube_body(area.values_id, columns, code)
        )
        rows = result_rows(body)
        if len(rows) >= CUBE_ROW_LIMIT:
            raise ValueError(
                f"{len(rows)} dimension rows reach the row limit {CUBE_ROW_LIMIT}"
            )
        try:
            describable = parse_cube(rows, columns) is not None
        except ValueError as error:  # stored anyway; parse reports it
            logger.warning("%s|%s: dimension rows not understood: %s", area.code, code, error)
            describable = True
        if not describable:
            logger.info(
                "%s|%s: no Riket or Region rows; stored, the current mapping cannot "
                "describe it",
                area.code,
                code,
            )
        link = resolve_link(
            self.discovery, area.code, code, row.get("HrefIndicatorDashboard")
        )
        log = logger.warning if link.resolution == "unresolved" else logger.debug
        log(
            "%s|%s: %d dimension rows; web page resolved by %s (dashboard %s)",
            area.code,
            code,
            len(rows),
            link.resolution,
            link.dashboard.slug if link.dashboard is not None else "none",
        )
        derived = {
            "row": row,
            "columns": dict(columns),
            "area": asdict(area),
            "link": link_record(link),
            "subject_label": self.subject_label(area, row),
        }
        return {"cube": body}, derived, describable

    async def build(
        self, area: Area, code: str, row: dict, columns: dict[str, str]
    ) -> tuple[dict | None, str]:
        """The document built in memory and its link resolution (typed boundary);
        ``(None, "")`` when the current mapping cannot describe the indicator."""
        responses, derived, describable = await self.fetch(area, code, row, columns)
        if not describable:
            return None, ""
        document = document_from(f"{area.code}|{code}", responses, derived)
        return document, derived["link"]["resolution"]

    def subject_label(self, area: Area, row: dict) -> str:
        if isinstance(row.get("AreaName"), str) and row["AreaName"].strip():
            return row["AreaName"].strip()
        overview = overview_dashboard(self.discovery, area.code)
        return overview.title if overview and overview.title else area.code


async def discover_provider(provider: dict, *, client=None) -> Discovery:
    """The current chart and dashboard listings, interpreted."""
    logger.info("Discovering the current analysverktyg listings")
    connection = client or DataanalysClient(provider)
    try:
        return await discover(connection)
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> list[dict]:
    """Build and validate selected indicators of one area.

    ``record`` holds ``area`` and ``indicators`` (``MBusinessKey`` codes) and, to
    avoid listing again, a ``discovery`` from :func:`discover_provider`.
    """
    discovery = record.get("discovery")
    if discovery is None:
        logger.info("No discovery supplied for %s; listing again", record["area"])
        discovery = await discover(client)
    area = discovery.areas[record["area"]]
    logger.info(
        "Building %d requested indicators of area %s",
        len(record["indicators"]),
        area.code,
    )
    harvester = _Harvester(client, discovery)
    catalogue = await harvester.catalogue(area)
    columns = await harvester.columns(area)
    rows = dict(catalogue.rows)
    documents = []
    for code in record["indicators"]:
        if code not in rows:
            raise LookupError(f"{area.code} catalogue does not list {code}")
        document, _ = await harvester.build(area, code, rows[code], columns)
        if document is None:
            raise LookupError(f"{area.code}|{code} has no Riket or Region rows")
        documents.append(document)
    logger.info("Built %d documents for area %s", len(documents), area.code)
    return documents


async def run(
    provider: dict,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    areas=None,
    indicators=None,
    languages=None,
) -> dict:
    """Rebuild every (or each selected) area's indicators into a worker output.

    ``areas`` selects area codes such as ``GONV``; ``indicators`` selects
    ``MBusinessKey`` codes (``G082``) or dataset codes (``GONV|G082``). Either makes
    the run bounded: it removes nothing.
    """
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured) or set(configured) - set(LANGUAGES):
        raise ValueError(f"Unsupported analysverktyg languages: {selected_languages}")
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not _allowed_path(relative) for relative in baseline):
        raise ValueError("Unexpected file in published analysverktyg data")
    state = load_state(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    store = SourceStore(output / "source", ADAPTER)
    # Placeholders stand in for documents: presence means a stored source entry.
    published, withheld = {}, {}
    for stored in store.entries():
        key = state_key(stored)
        status = state["datasets"].get(key, {}).get("status")
        (withheld if status == "withheld" else published)[key] = True
    logger.info(
        "Baseline in %s: %d published entries, %d withheld, %d state entries, "
        "mapping version %s (current %s)",
        data,
        len(published),
        len(withheld),
        len(state["datasets"]),
        state.get("mapping_version"),
        MAPPING_VERSION,
    )
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    max_age = timedelta(days=provider.get("max_age_days", 60))
    cooldown = provider.get("retry_cooldown_seconds", 60)
    connection = client or DataanalysClient(provider)
    progress = Progress(ADAPTER, PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors: list[dict] = []
    report: dict = {
        "areas": [],
        "areas_incomplete": {},
        "unsupported_datasources": [],
        "links_unresolved": [],
        "out_of_scope": [],
    }
    bounded = areas is not None or indicators is not None
    wanted = {name.casefold() for name in indicators} if indicators else None
    found: set[str] = set()
    logger.info(
        "Run start: %s; areas=%s indicators=%s languages=%s max_age=%dd "
        "retry_cooldown=%ss request_interval=%ss; the site has no change signal, "
        "so every selected indicator is refetched (no refresh selection)",
        "bounded (removes nothing)" if bounded else "full",
        _sample(sorted(areas)) if areas is not None else "all",
        _sample(sorted(indicators)) if indicators else "all",
        selected_languages,
        max_age.days,
        cooldown,
        provider.get("request_interval_seconds", 1.0),
    )

    def matches(key: str) -> bool:
        if wanted is None:
            return True
        code = key.split("/", 1)[1]
        return bool({code.casefold(), indicator_of(key).casefold()} & wanted)

    def fail(stage, resource, error, language="sv", *, emit=True, url=None):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        if url is not None:
            entry["url"] = url
        errors.append(entry)
        if emit:
            logger.error("%s failed at %s: %s", resource, stage, entry["error"])
            progress.error(
                stage,
                resource,
                language,
                entry["error"],
                **({"url": url} if url else {}),
            )
        return entry

    def known(area_code=None):
        keys = set(published) | set(withheld) | set(state["datasets"])
        return {
            key
            for key in keys
            if key.split("/", 1)[0] in selected_languages
            and (area_code is None or area_of(key) == area_code)
            and matches(key)
        }

    def remove(key, reason):
        logger.info("Removing %s: %s", key, reason)
        settled.discard(key)
        state["datasets"].pop(key, None)
        failures.pop(key, None)
        unresolved.discard(key)
        store.remove(stored_key(key))
        if (published.pop(key, None) or withheld.pop(key, None)) is not None:
            counts["removed"] += 1

    working: dict[str, bool] = {}
    failures: dict[str, tuple[str, Exception]] = {}
    settled: set[str] = set()
    outcomes: dict[str, str] = {}
    unresolved: set[str] = set()
    out_of_scope: set[str] = set()
    catalogue_problems: dict[str, list[tuple[int, Exception]]] = {}
    unsupported_areas: dict[str, str] = {}

    def record_failure(key: str, stage: str, error: Exception, *, url=DATA_URL):
        """Publish the attempt immediately; only unresolved attempts enter reports."""
        failures[key] = (stage, error)
        logger.error("%s failed at %s: %s: %s", key, stage, type(error).__name__, error)
        progress.error(stage, key, "sv", f"{type(error).__name__}: {error}", url=url)
        progress.update(failed=len(failures))

    try:
        progress.update(phase="discovery", current="charts")
        listing_bodies: dict = {}
        discovery = await discover(connection, listing_bodies)
        for name, pages in listing_bodies.items():
            if pages:
                store.put_shared(
                    PROVIDER_CODE, f"{name}.json", pages, retrieved_at=stamp
                )
        for resource, error in discovery.problems:
            fail("discovery", resource, error)
        if not discovery.charts_complete and not any(
            r == "charts" for r, _ in discovery.problems
        ):
            fail(
                "discovery",
                "charts",
                ValueError(
                    f"received {discovery.charts_listed} chart rows, "
                    f"{discovery.charts_count} listed"
                ),
            )
        if not discovery.dashboards_complete and not any(
            r == "dashboards" for r, _ in discovery.problems
        ):
            fail(
                "discovery",
                "dashboards",
                ValueError(
                    f"received {discovery.dashboards_listed} dashboard rows, "
                    f"{discovery.dashboards_count} listed"
                ),
            )
        listed_areas = sorted(set(discovery.areas) | set(discovery.incomplete))
        logger.info(
            "Listed areas (%d): %s", len(listed_areas), _sample(listed_areas, 30)
        )
        if not listed_areas and discovery.charts_complete:
            fail(
                "discovery",
                "charts",
                ValueError("The chart list names no analysis area tables"),
            )
            discovery.charts_complete = False
        report.update(
            areas=sorted(discovery.areas),
            areas_incomplete=dict(sorted(discovery.incomplete.items())),
            unsupported_datasources=discovery.unsupported,
        )
        charts_whole = discovery.charts_complete and not any(
            resource.startswith("charts/") for resource, _ in discovery.problems
        )
        if areas is None:
            selected = listed_areas
            logger.info("Selected all %d listed areas", len(selected))
        else:
            names = {name.casefold() for name in areas}
            selected = [a for a in listed_areas if a.casefold() in names]
            logger.info(
                "Selected %d of %d listed areas by the areas argument: %s",
                len(selected),
                len(listed_areas),
                _sample(selected),
            )
            for name in sorted(names - {a.casefold() for a in listed_areas}):
                fail("discovery", name, LookupError("Unknown analysis area"))
        if not bounded and charts_whole:
            for key in sorted(known()):
                if area_of(key) not in listed_areas:
                    remove(key, "its area is no longer in the complete chart list")
        elif bounded:
            logger.info("Bounded run: unlisted areas and indicators are not removed")
        else:
            logger.warning(
                "Chart list incomplete: removals are not proven, so no dataset "
                "will be removed"
            )

        harvester = _Harvester(connection, discovery)

        def accept(key: str, responses: dict, derived: dict, row: dict) -> None:
            stored = store.get(stored_key(key))
            store.put(
                stored_key(key),
                responses=responses,
                derived=derived,
                updated=row.get("DataGenerationDate"),
                retrieved_at=stamp,
            )
            failures.pop(key, None)
            progress.update(failed=len(failures))
            working[key] = True
            resolution = derived["link"]["resolution"]
            (unresolved.add if resolution == "unresolved" else unresolved.discard)(key)
            unchanged = (
                stored is not None
                and stored["responses"] == responses
                and stored["derived"] == derived
            )
            outcomes.setdefault(key, "unchanged" if unchanged else "rebuilt")
            state["datasets"][key].update(
                verified_at=stamp, mapping_version=MAPPING_VERSION, error=None
            )
            (withheld if key in withheld else published)[key] = True

        async def harvest(area_code: str) -> bool:
            """Process one area; return whether an endpoint failure occurred."""
            previous = known(area_code)
            logger.debug(
                "Area %s: %d datasets known from earlier runs", area_code, len(previous)
            )
            settled.update(previous)
            for key in previous:
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
            if area_code in discovery.incomplete:
                logger.warning(
                    "Area %s: tables not listed exactly once; its %d known datasets "
                    "stay as they are and are treated as working",
                    area_code,
                    len(previous),
                )
                for key in previous:
                    working[key] = True
                record_failure(
                    f"*/{area_code}",
                    "metadata",
                    ValueError(discovery.incomplete[area_code]),
                    url=CHART_LIST_URL,
                )
                return False
            area = discovery.areas[area_code]
            try:
                catalogue = await harvester.catalogue(area)
                columns = await harvester.columns(area)
            except UnsupportedLayout as error:
                if not previous:  # never harvested: out of the model, not a failure
                    logger.info(
                        "Area %s is outside the model and was never harvested; "
                        "skipped without a failure: %s",
                        area_code,
                        error,
                    )
                    unsupported_areas[area_code] = str(error)
                    return False
                logger.warning(
                    "Area %s changed to an unsupported layout; its %d known "
                    "datasets stay as they are",
                    area_code,
                    len(previous),
                )
                for key in previous:
                    working[key] = True
                record_failure(f"*/{area_code}", "metadata", error)
                return False
            except Exception as error:
                stage = "endpoint" if isinstance(error, Unreachable) else "metadata"
                logger.warning(
                    "Area %s: %s failure while reading its catalogue and columns; "
                    "its %d known datasets are treated as %s",
                    area_code,
                    stage,
                    len(previous),
                    "unavailable (withheld)" if stage == "endpoint" else "working",
                )
                for key in previous:  # reported once, for the area
                    working[key] = stage == "metadata"
                record_failure(f"*/{area_code}", stage, error)
                return stage == "endpoint"
            failures.pop(f"*/{area_code}", None)
            progress.update(failed=len(failures))
            store.put_shared(
                PROVIDER_CODE,
                f"catalogue/{area_code}.json",
                harvester.catalogue_bodies[area_code],
                retrieved_at=stamp,
            )
            store.put_shared(
                PROVIDER_CODE,
                f"columns/{area_code}.json",
                harvester.columns_bodies[area_code],
                retrieved_at=stamp,
            )
            catalogue_problems[area_code] = catalogue.problems
            current = set()
            tally = dict.fromkeys(
                ("skipped", "rebuilt", "unchanged", "out_of_scope", "unreachable"), 0
            )
            rejected = 0
            for code in catalogue.duplicates:
                key = f"sv/{area_code}|{code}"
                if not matches(key):
                    continue
                found.add(key)
                current.add(key)
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                working[key] = True
                rejected += 1
                record_failure(
                    key,
                    "metadata",
                    ValueError("listed more than once in the catalogue; quarantined"),
                )
            endpoint_failed = False
            for code, row in catalogue.rows:
                key = f"sv/{area_code}|{code}"
                if not matches(key):
                    tally["skipped"] += 1
                    logger.debug(
                        "%s: skipped, not matched by the indicator filter", key
                    )
                    continue
                found.add(key)
                current.add(key)
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                progress.update(current=key, operation="rebuild")
                try:
                    responses, derived, describable = await harvester.fetch(
                        area, code, row, columns
                    )
                except Unreachable as error:
                    working[key] = False
                    tally["unreachable"] += 1
                    record_failure(key, "endpoint", error)
                    endpoint_failed = True
                    continue
                except Exception as error:  # the values query answered
                    working[key] = True
                    rejected += 1
                    record_failure(key, "metadata", error)
                    continue
                if not describable:  # stored all the same: never dismissed
                    tally["out_of_scope"] += 1
                    out_of_scope.add(key)
                else:
                    out_of_scope.discard(key)
                try:
                    accept(key, responses, derived, row)
                except Rejected as error:
                    working[key] = True
                    rejected += 1
                    record_failure(key, "metadata", error)
                    continue
                tally[outcomes[key]] += 1
                logger.debug("%s: accepted, %s", key, outcomes[key])
                progress.update(succeeded=len(outcomes))
            logger.info(
                "Area %s: %d known; %d in catalogue, %d skipped by the indicator "
                "filter, %d rebuilt, %d unchanged, %d out of scope, %d rejected, "
                "%d unreachable",
                area_code,
                len(previous),
                len(catalogue.rows) + len(catalogue.duplicates),
                tally["skipped"],
                tally["rebuilt"],
                tally["unchanged"],
                tally["out_of_scope"],
                rejected,
                tally["unreachable"],
            )
            if not bounded and charts_whole and catalogue.complete:
                for key in sorted(previous - current):
                    remove(key, "absent from the complete catalogue")
            else:
                # An absent row in an incomplete or bounded run was not checked.
                # Leave its accepted publication/state untouched by currency rules.
                if previous - current:
                    logger.info(
                        "Area %s: %d known datasets are absent from the catalogue "
                        "or filter and are left untouched (%s)",
                        area_code,
                        len(previous - current),
                        "bounded run"
                        if bounded
                        else "chart list incomplete"
                        if not charts_whole
                        else "catalogue has malformed rows",
                    )
                settled.difference_update(previous - current)
            return endpoint_failed

        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, area_code in enumerate(selected, start=1):
            logger.info("Area %d/%d: %s", number, len(selected), area_code)
            progress.update(current=area_code, operation="rebuild")
            if await harvest(area_code):
                retry.append(area_code)
            progress.update(processed=number)
        if retry:
            logger.warning(
                "Endpoint failures in %d areas (%s); retrying them once after a "
                "%ss cooldown",
                len(retry),
                _sample(retry),
                cooldown,
            )
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for area_code in retry:
                logger.info("Retrying area %s", area_code)
                progress.update(current=area_code, operation="retry")
                await harvest(area_code)
        for area_code, problems in sorted(catalogue_problems.items()):
            for position, error in problems:
                fail("discovery", f"catalogue/{area_code}/{position}", error)
        if wanted is not None and not any(f"*/{a}" in failures for a in selected):
            matched = {
                name
                for key in found
                for name in (
                    key.split("/", 1)[1].casefold(),
                    indicator_of(key).casefold(),
                )
            }
            for name in sorted(wanted - matched):
                fail("discovery", name, LookupError("Unknown indicator"))
        for key, (stage, error) in sorted(failures.items()):
            entry = fail(
                stage,
                key,
                error,
                emit=False,
                url=CHART_LIST_URL
                if key.startswith("*/") and key[2:] in discovery.incomplete
                else DATA_URL,
            )
            counts["failed"] += 1
            affected = known(key[2:]) if key.startswith("*/") else [key]
            for name in affected:
                if name in state["datasets"]:
                    state["datasets"][name]["error"] = entry
        for outcome in outcomes.values():
            counts[outcome] += 1
        counts["out_of_scope"] = len(out_of_scope)
        progress.update(failed=counts["failed"])
        was_withheld = set(withheld)
        logger.info(
            "Settling %d checked datasets: published only while the endpoint "
            "worked and verification is at most %d days old",
            len(settled),
            max_age.days,
        )
        settle(
            sorted(settled),
            published,
            withheld,
            state["datasets"],
            working,
            now,
            max_age,
            counts,
        )
        for reason in ("endpoint unavailable", "stale"):
            newly = sorted(
                key
                for key in set(withheld) - was_withheld
                if state["datasets"].get(key, {}).get("withheld_reason") == reason
            )
            if newly:
                logger.warning(
                    "Withheld %d datasets (%s): %s", len(newly), reason, _sample(newly)
                )
        restored = sorted(was_withheld & set(published))
        if restored:
            logger.info(
                "Restored %d withheld datasets: %s", len(restored), _sample(restored)
            )
    finally:
        if client is None:
            await connection.close()

    report.update(
        areas_unsupported=dict(sorted(unsupported_areas.items())),
        links_unresolved=sorted(unresolved),
        out_of_scope=sorted(out_of_scope),
    )
    state["mapping_version"] = MAPPING_VERSION
    store.save()
    save(output, state)
    status = report_status(pending=0, errors=errors)
    if unsupported_areas:
        logger.info(
            "Areas outside the model: %s",
            "; ".join(f"{a}: {r}" for a, r in sorted(unsupported_areas.items())),
        )
    if unresolved:
        logger.warning(
            "%d indicators have no resolvable web page: %s",
            len(unresolved),
            _sample(sorted(unresolved)),
        )
    logger.info(
        "Run finished: status=%s counts=%s published=%d withheld=%d requests=%s "
        "errors=%d",
        status,
        counts,
        len(published),
        len(withheld),
        getattr(connection, "request_count", None),
        len(errors),
    )
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "selection": {
            "areas": sorted(areas) if areas else None,
            "indicators": sorted(indicators) if indicators else None,
            "languages": selected_languages,
        },
        "counts": counts,
        "published": {
            language: sum(k.startswith(f"{language}/") for k in published)
            for language in configured
        },
        "withheld": sorted(withheld),
        "requests": getattr(connection, "request_count", None),
        **report,
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting %d files of run %s (status %s) from %s into %s",
        len(result["manifest"]),
        result.get("run_id"),
        result.get("status"),
        output,
        data,
    )
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale analysverktyg artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Analysverktyg artifact checksum mismatch")
    if not {STATE, REPORT} <= set(result["manifest"]) or not all(
        _allowed_path(relative) for relative in result["manifest"]
    ):
        raise ValueError("Unexpected analysverktyg artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
        logger.debug("Collected %s", relative)
    for relative in harvest_manifest(data):
        if relative.startswith("source/") and relative not in result["manifest"]:
            (data / relative).unlink()
    return result

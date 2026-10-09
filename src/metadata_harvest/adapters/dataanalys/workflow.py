"""Weekly analysverktyg rebuild with independent area and indicator failures.

The site publishes no change signal that covers the dimension categories, and a full
harvest is about 170 requests, so every run rebuilds every indicator; an identical
rebuild keeps its accepted bytes. Discovery reads the chart list (area tables and
the charts' ``Matt_id`` filters) and the dashboard list (web pages). Per area, one
catalogue query lists the indicators and one probe learns the values table's
columns; per indicator, one query on the values table gives the dimension
categories and is its endpoint check. An indicator is published only while that
endpoint works and its document was verified within ``max_age_days``; otherwise its
last good document is withheld in state. Only a complete chart list in an
unbounded run proves an indicator's removal.
"""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from metadata_harvest.adapters.dataanalys.api import (
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
from metadata_harvest.adapters.dataanalys.client import (
    DataanalysClient,
    RequestRejected,
    Unreachable,
)
from metadata_harvest.adapters.dataanalys.mapping import MAPPING_VERSION, build_dataset
from metadata_harvest.adapters.dataanalys.source import (
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
from metadata_harvest.adapters.dataanalys.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    collection,
    load_documents,
    load_state,
    save,
)
from metadata_harvest.artifacts import check_collection_sizes, manifest, report_status
from metadata_harvest.currency import settle
from metadata_harvest.progress import Progress
from metadata_harvest.storage import read, write
from metadata_harvest.validation import validate_metadata

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


def area_of(key: str) -> str:
    """The area code of a ``<language>/<AREA>|<indicator>`` key."""
    return key.split("/", 1)[1].split("|", 1)[0]


def indicator_of(key: str) -> str:
    return key.split("/", 1)[1].split("|", 1)[1]


async def _list(
    client, url: str, columns, resource: str
) -> tuple[list, int, Exception | None]:
    """Retain understood pages when a later page fails; stop only this scope."""
    rows: list = []
    count = 0
    for page in range(MAX_PAGES):
        try:
            page_count, result = check_page(
                await client.get_json(url, {"q": list_query(columns, page)}), resource
            )
            if page and page_count != count:
                raise ValueError(
                    f"{resource} listing count changed from {count} to {page_count}"
                )
            count = page_count
        except Exception as error:
            return rows, count, error
        rows += result
        if len(result) < PAGE_SIZE or len(rows) >= count:
            break
    return rows, count, None


async def discover(client) -> Discovery:
    """Read the chart and dashboard lists.

    Page failures are recorded independently; cancellation still propagates.
    """
    discovery = Discovery()
    charts, discovery.charts_count, error = await _list(
        client, CHART_LIST_URL, CHART_COLUMNS, "chart"
    )
    discovery.charts_listed = len(charts)
    discovery.charts_complete = error is None and listing_complete(
        charts, discovery.charts_count
    )
    if error is not None:
        discovery.problems.append(("charts", error))
    add_charts(discovery, charts)
    dashboards, discovery.dashboards_count, error = await _list(
        client, DASHBOARD_LIST_URL, DASHBOARD_COLUMNS, "dashboard"
    )
    discovery.dashboards_listed = len(dashboards)
    discovery.dashboards_complete = error is None and listing_complete(
        dashboards, discovery.dashboards_count
    )
    if error is not None:
        discovery.problems.append(("dashboards", error))
    add_dashboards(discovery, dashboards)
    return discovery


class UnsupportedLayout(ValueError):
    """An area table lacks a column the adapter's model requires."""


class _Harvester:
    """Catalogue, column probe and document builds for areas of one discovery."""

    def __init__(self, client, discovery: Discovery):
        self.client, self.discovery = client, discovery
        self.columns_cache: dict[str, dict[str, str]] = {}

    async def catalogue(self, area: Area) -> Catalogue:
        """The area's catalogue; optional columns the table lacks are dropped once.

        Raises:
            Unreachable: The catalogue query did not answer.
            UnsupportedLayout: A required catalogue column is missing.
            ValueError: The catalogue may be truncated.
        """
        columns = [*CATALOGUE_REQUIRED, *CATALOGUE_OPTIONAL]
        for attempt in (1, 2):
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
                continue
            rows = result_rows(body)
            if len(rows) >= CATALOGUE_ROW_LIMIT:
                raise ValueError(
                    f"{area.catalogue_dataset}: {len(rows)} catalogue rows reach the "
                    f"row limit {CATALOGUE_ROW_LIMIT}; the catalogue may be truncated"
                )
            return parse_catalogue(rows, columns)
        raise AssertionError("unreachable")

    async def columns(self, area: Area) -> dict[str, str]:
        """Logical ``Trender`` column to source column, learnt by a one-row probe.

        Raises:
            Unreachable: The probe did not answer.
            UnsupportedLayout: A required logical column has no candidate.
        """
        if area.code in self.columns_cache:
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
                continue
            result_rows(body)
            self.columns_cache[area.code] = current
            return current
        raise ValueError(f"{area.values_dataset}: columns could not be resolved")

    async def build(
        self, area: Area, code: str, row: dict, columns: dict[str, str]
    ) -> tuple[dict | None, str]:
        """The validated document and its link resolution; ``(None, "")`` when the
        indicator has no Riket or Region rows.

        Raises:
            Unreachable: The values query (the endpoint check) did not answer.
        """
        rows = result_rows(
            await self.client.post_json(
                DATA_URL, cube_body(area.values_id, columns, code)
            )
        )
        if len(rows) >= CUBE_ROW_LIMIT:
            raise ValueError(
                f"{len(rows)} dimension rows reach the row limit {CUBE_ROW_LIMIT}"
            )
        cube = parse_cube(rows, columns)
        if cube is None:
            return None, ""
        link = resolve_link(
            self.discovery, area.code, code, row.get("HrefIndicatorDashboard")
        )
        document = build_dataset(
            area,
            code,
            row,
            cube,
            columns,
            link,
            subject_label=self.subject_label(area, row),
        )
        validate_metadata(document, adapter=ADAPTER)
        return document, link.resolution

    def subject_label(self, area: Area, row: dict) -> str:
        if isinstance(row.get("AreaName"), str) and row["AreaName"].strip():
            return row["AreaName"].strip()
        overview = overview_dashboard(self.discovery, area.code)
        return overview.title if overview and overview.title else area.code


async def discover_provider(provider: dict, *, client=None) -> Discovery:
    """The current chart and dashboard listings, interpreted."""
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
    discovery = record.get("discovery") or await discover(client)
    area = discovery.areas[record["area"]]
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
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published analysverktyg data")
    state = load_state(data)
    published = load_documents(data, configured)
    withheld = state["withheld"]
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
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

    def remove(key):
        settled.discard(key)
        state["datasets"].pop(key, None)
        failures.pop(key, None)
        unresolved.discard(key)
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
        progress.error(stage, key, "sv", f"{type(error).__name__}: {error}", url=url)
        progress.update(failed=len(failures))

    try:
        progress.update(phase="discovery", current="charts")
        discovery = await discover(connection)
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
        else:
            names = {name.casefold() for name in areas}
            selected = [a for a in listed_areas if a.casefold() in names]
            for name in sorted(names - {a.casefold() for a in listed_areas}):
                fail("discovery", name, LookupError("Unknown analysis area"))
        if not bounded and charts_whole:
            for key in sorted(known()):
                if area_of(key) not in listed_areas:
                    remove(key)

        harvester = _Harvester(connection, discovery)

        def accept(key: str, document: dict, resolution: str) -> None:
            failures.pop(key, None)
            progress.update(failed=len(failures))
            working[key] = True
            out_of_scope.discard(key)
            (unresolved.add if resolution == "unresolved" else unresolved.discard)(key)
            accepted = published.get(key) or withheld.get(key)
            outcomes.setdefault(key, "unchanged" if document == accepted else "rebuilt")
            state["datasets"][key].update(
                verified_at=stamp, mapping_version=MAPPING_VERSION, error=None
            )
            (withheld if key in withheld else published)[key] = document

        async def harvest(area_code: str) -> bool:
            """Process one area; return whether an endpoint failure occurred."""
            previous = known(area_code)
            settled.update(previous)
            for key in previous:
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
            if area_code in discovery.incomplete:
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
                    unsupported_areas[area_code] = str(error)
                    return False
                for key in previous:
                    working[key] = True
                record_failure(f"*/{area_code}", "metadata", error)
                return False
            except Exception as error:
                stage = "endpoint" if isinstance(error, Unreachable) else "metadata"
                for key in previous:  # reported once, for the area
                    working[key] = stage == "metadata"
                record_failure(f"*/{area_code}", stage, error)
                return stage == "endpoint"
            failures.pop(f"*/{area_code}", None)
            progress.update(failed=len(failures))
            catalogue_problems[area_code] = catalogue.problems
            current = set()
            for code in catalogue.duplicates:
                key = f"sv/{area_code}|{code}"
                if not matches(key):
                    continue
                found.add(key)
                current.add(key)
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                working[key] = True
                record_failure(
                    key,
                    "metadata",
                    ValueError("listed more than once in the catalogue; quarantined"),
                )
            endpoint_failed = False
            for code, row in catalogue.rows:
                key = f"sv/{area_code}|{code}"
                if not matches(key):
                    continue
                found.add(key)
                current.add(key)
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                progress.update(current=key, operation="rebuild")
                try:
                    document, resolution = await harvester.build(
                        area, code, row, columns
                    )
                except Unreachable as error:
                    working[key] = False
                    record_failure(key, "endpoint", error)
                    endpoint_failed = True
                    continue
                except Exception as error:  # the values query answered
                    working[key] = True
                    record_failure(key, "metadata", error)
                    continue
                if document is None:
                    out_of_scope.add(key)
                    if not bounded and charts_whole and catalogue.complete:
                        current.discard(key)
                        remove(key)
                    else:
                        working[key] = True
                    continue
                accept(key, document, resolution)
                progress.update(succeeded=len(outcomes))
            if not bounded and charts_whole and catalogue.complete:
                for key in sorted(previous - current):
                    remove(key)
            else:
                # An absent row in an incomplete or bounded run was not checked.
                # Leave its accepted publication/state untouched by currency rules.
                settled.difference_update(previous - current)
            return endpoint_failed

        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, area_code in enumerate(selected, start=1):
            progress.update(current=area_code, operation="rebuild")
            if await harvest(area_code):
                retry.append(area_code)
            progress.update(processed=number)
        if retry:
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for area_code in retry:
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
    finally:
        if client is None:
            await connection.close()

    report.update(
        areas_unsupported=dict(sorted(unsupported_areas.items())),
        links_unresolved=sorted(unresolved),
        out_of_scope=sorted(out_of_scope),
    )
    state["mapping_version"] = MAPPING_VERSION
    save(output, state, published, configured)
    status = report_status(pending=0, errors=errors)
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
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale analysverktyg artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Analysverktyg artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        raise ValueError("Unexpected analysverktyg artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    return result

"""Weekly analysverktyg rebuild with independent area and indicator failures.

The site publishes no change signal that covers the dimension categories, and a
full harvest is about 170 requests, so every run refetches every indicator;
identical responses keep their stored bytes. Discovery reads the chart list (area
tables and the charts' ``Matt_id`` filters) and the dashboard list (web pages);
both are stored as shared source files. Each area is one scope: its catalogue and
column probe are its discovery, stored as shared files too. Per indicator, one
query on the values table gives the dimension rows, is its endpoint check and is
stored as its envelope together with the catalogue row, columns, area and link it
was issued for. Every listed indicator is stored, whether or not the current
mapping can describe it.
"""

from dataclasses import asdict

from apps.harvest.metadata_adapters.dataanalys.client import (
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
    PROVIDER_CODE,
    TRENDER_COLUMNS,
    DataanalysClient,
    RequestRejected,
    catalogue_body,
    cube_body,
    list_query,
    missing_columns,
    probe_body,
    result_rows,
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
from harvest.lifecycle import Adapter, Run, Scope, Stored, Task

MAX_PAGES = 50
"""Stop paging a list after this many pages (5,000 rows): a runaway guard."""


def area_of(key: str) -> str:
    """The area code of a ``<language>/<AREA>|<indicator>`` key."""
    return key.split("/", 1)[1].split("|", 1)[0]


def indicator_of(key: str) -> str:
    return key.split("/", 1)[1].split("|", 1)[1]


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
            return rows, count, error
        rows += result
        if pages is not None:
            pages.append(body)
        if len(result) < PAGE_SIZE or len(rows) >= count:
            break
    return rows, count, None


async def read_listings(client, bodies: dict | None = None) -> Discovery:
    """Read the chart and dashboard lists.

    Page failures are recorded independently; cancellation still propagates.
    ``bodies`` receives ``{"charts": [...pages], "dashboards": [...pages]}``.
    """
    discovery = Discovery()
    bodies = {} if bodies is None else bodies
    bodies.update(charts=[], dashboards=[])
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
    return discovery


class UnsupportedLayout(ValueError):
    """An area table lacks a column the adapter's model requires."""


class Harvester:
    """Catalogue, column probe and indicator queries for areas of one discovery."""

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
            catalogue = parse_catalogue(rows, columns)
            self.catalogue_bodies[area.code] = {"columns": list(columns), "body": body}
            return catalogue
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
            self.columns_bodies[area.code] = {"columns": dict(current), "body": body}
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
        except ValueError:  # stored anyway; parse reports it
            describable = True
        link = resolve_link(
            self.discovery, area.code, code, row.get("HrefIndicatorDashboard")
        )
        derived = {
            "row": row,
            "columns": dict(columns),
            "area": asdict(area),
            "link": link_record(link),
            "subject_label": self.subject_label(area, row),
        }
        return {"cube": body}, derived, describable

    def subject_label(self, area: Area, row: dict) -> str:
        if isinstance(row.get("AreaName"), str) and row["AreaName"].strip():
            return row["AreaName"].strip()
        overview = overview_dashboard(self.discovery, area.code)
        return overview.title if overview and overview.title else area.code


async def build_documents(client, record: dict) -> list[dict]:
    """Documents of selected indicators of one area, built from fresh requests
    (tests and live checks). ``record`` holds ``area`` and ``indicators`` and,
    to avoid listing again, a ``discovery`` from :func:`read_listings`."""
    discovery = record.get("discovery") or await read_listings(client)
    area = discovery.areas[record["area"]]
    harvester = Harvester(client, discovery)
    catalogue = await harvester.catalogue(area)
    columns = await harvester.columns(area)
    rows = dict(catalogue.rows)
    documents = []
    for code in record["indicators"]:
        if code not in rows:
            raise LookupError(f"{area.code} catalogue does not list {code}")
        responses, derived, describable = await harvester.fetch(
            area, code, rows[code], columns
        )
        if not describable:
            raise LookupError(f"{area.code}|{code} has no Riket or Region rows")
        documents.append(document_from(f"{area.code}|{code}", responses, derived))
    return documents


async def discover(run: Run) -> list[Scope]:
    bodies: dict = {}
    discovery = await read_listings(run.client, bodies)
    for name, pages in bodies.items():
        if pages:
            run.store.put_shared(
                PROVIDER_CODE, f"{name}.json", pages, retrieved_at=run.stamp
            )
    for resource, error in discovery.problems:
        run.fail("discovery", resource, error)
    for name, complete, listed, count in (
        (
            "charts",
            discovery.charts_complete,
            discovery.charts_listed,
            discovery.charts_count,
        ),
        (
            "dashboards",
            discovery.dashboards_complete,
            discovery.dashboards_listed,
            discovery.dashboards_count,
        ),
    ):
        if not complete and not any(r == name for r, _ in discovery.problems):
            run.fail(
                "discovery",
                name,
                ValueError(f"received {listed} {name[:-1]} rows, {count} listed"),
            )
    listed_areas = sorted(set(discovery.areas) | set(discovery.incomplete))
    if not listed_areas and discovery.charts_complete:
        run.fail(
            "discovery",
            "charts",
            ValueError("The chart list names no analysis area tables"),
        )
        discovery.charts_complete = False
    run.report.update(
        areas=sorted(discovery.areas),
        areas_incomplete=dict(sorted(discovery.incomplete.items())),
        areas_unsupported={},
        unsupported_datasources=discovery.unsupported,
        links_unresolved=[],
        out_of_scope=[],
    )
    run.removals = discovery.charts_complete and not any(
        resource.startswith("charts/") for resource, _ in discovery.problems
    )
    areas = run.selection.get("areas")
    if areas is None:
        selected = listed_areas
    else:
        names = {name.casefold(): name for name in areas}
        selected = [a for a in listed_areas if a.casefold() in names]
        for name in sorted(names.keys() - {a.casefold() for a in listed_areas}):
            run.fail(
                "discovery", f"*/{names[name]}", LookupError("Unknown analysis area")
            )
    indicators = run.selection.get("indicators")
    wanted = {name.casefold() for name in indicators} if indicators else None
    run.state["mapping_version"] = MAPPING_VERSION
    harvester = Harvester(run.client, discovery)
    found: set[str] = set()
    loaded: set[str] = set()

    def matches(key: str) -> bool:
        if wanted is None:
            return True
        return bool(
            {key.split("/", 1)[1].casefold(), indicator_of(key).casefold()} & wanted
        )

    scopes = []
    for area_code in selected:
        scope = Scope(area_code, None)

        async def load(area_code=area_code, scope=scope) -> list[Task]:
            if area_code in discovery.incomplete:
                raise ValueError(discovery.incomplete[area_code])
            area = discovery.areas[area_code]
            try:
                catalogue = await harvester.catalogue(area)
                columns = await harvester.columns(area)
            except UnsupportedLayout as error:
                if run.known(area_code):  # the layout changed under known datasets
                    raise
                run.report["areas_unsupported"][area_code] = str(error)
                scope.complete = False  # outside the model: nothing to remove
                loaded.add(area_code)
                return []
            run.store.put_shared(
                PROVIDER_CODE,
                f"catalogue/{area_code}.json",
                harvester.catalogue_bodies[area_code],
                retrieved_at=run.stamp,
            )
            run.store.put_shared(
                PROVIDER_CODE,
                f"columns/{area_code}.json",
                harvester.columns_bodies[area_code],
                retrieved_at=run.stamp,
            )
            for position, error in catalogue.problems:
                run.fail("discovery", f"catalogue/{area_code}/{position}", error)
            scope.complete = catalogue.complete
            tasks = []
            for code in catalogue.duplicates:
                key = f"sv/{area_code}|{code}"
                if matches(key):
                    found.add(key)
                    tasks.append(Task(key, ("duplicate",)))
            for code, row in catalogue.rows:
                key = f"sv/{area_code}|{code}"
                if matches(key):
                    found.add(key)
                    tasks.append(Task(key, (harvester, area, code, row, columns)))
            loaded.add(area_code)
            if wanted is not None and loaded >= set(selected):
                matched = {
                    name
                    for key in found
                    for name in (
                        key.split("/", 1)[1].casefold(),
                        indicator_of(key).casefold(),
                    )
                }
                for name in sorted(wanted - matched):
                    run.fail("discovery", f"*/{name}", LookupError("Unknown indicator"))
            return tasks

        scope.load = load
        scopes.append(scope)
    return scopes


async def fetch(run: Run, task: Task) -> Stored:
    if task.item[0] == "duplicate":
        raise ValueError("listed more than once in the catalogue; quarantined")
    harvester, area, code, row, columns = task.item
    responses, derived, describable = await harvester.fetch(area, code, row, columns)
    out_of_scope = run.report["out_of_scope"]
    if not describable:  # stored all the same: never dismissed
        if task.key not in out_of_scope:
            out_of_scope.append(task.key)
            out_of_scope.sort()
            run.counts["out_of_scope"] += 1
    unresolved = run.report["links_unresolved"]
    if derived["link"]["resolution"] == "unresolved":
        if task.key not in unresolved:
            unresolved.append(task.key)
            unresolved.sort()
    return Stored(
        responses=responses,
        derived=derived,
        updated=row.get("DataGenerationDate"),
        info={"mapping_version": MAPPING_VERSION},
    )


def forget(run: Run, key: str) -> None:
    """Drop an area's catalogue and column probe once none of its items remain."""
    area = area_of(key)
    if any(area_of(k) == area for k in run.known()):
        return
    run.store.remove_shared(PROVIDER_CODE, f"catalogue/{area}.json")
    run.store.remove_shared(PROVIDER_CODE, f"columns/{area}.json")


ADAPTER = Adapter(
    name="dataanalys",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-dataanalys.yml",
    client=lambda binding: DataanalysClient(binding.options),
    discover=discover,
    fetch=fetch,
    options={"subject": "areas", "indicator": "indicators"},
    languages=("sv",),
    counts=("out_of_scope",),
    forget=forget,
    scope_of=area_of,
    scope_failures=lambda report: [
        e["resource"]
        for e in report.get("errors") or []
        if (
            str(e.get("resource")).startswith("*/")
            and "Unknown" not in str(e.get("error"))
        )
        or (
            e.get("stage") == "discovery"
            and e.get("resource") in ("charts", "dashboards")
        )
    ],
)

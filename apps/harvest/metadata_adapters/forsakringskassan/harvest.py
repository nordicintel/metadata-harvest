"""Weekly Försäkringskassan refresh with independent report and table failures.

Försäkringskassan publishes no modification date, ``ETag`` or ``Last-Modified``
(``meta.revision`` is 0 everywhere), so the change signal is a hash: of each
report's ``meta.json`` and of the text entries its documents use. Discovery reads
``/reports`` and the texts once; each report is one scope whose discovery reads its
``meta.json`` (the endpoint check of its tables), all stored as shared source files.
A report whose hashes and mapping version match its last complete build keeps its
tables; otherwise each table is refetched: its catalogue row and, for tables with
undeclared columns, a one-row probe. A table whose rows carry dimensions
``meta.json`` does not describe is excluded. When the texts fail, no table is
refetched or verified and the failure is reported once (``discovery``/``texts``).
"""

from apps.harvest.metadata_adapters.forsakringskassan.client import (
    PROVIDER_CODE,
    REPORTS_URL,
    TEXTS_URL,
    ForsakringskassanClient,
    Unreachable,
    meta_url,
    table_url,
)
from apps.harvest.metadata_adapters.forsakringskassan.mapping import (
    LANGUAGE,
    MAPPING_VERSION,
)
from apps.harvest.metadata_adapters.forsakringskassan.parse import (
    meta_name,
    table_record,
)
from apps.harvest.metadata_adapters.forsakringskassan.source import (
    Listed,
    Report,
    Table,
    catalogue,
    fingerprint,
    parse_meta,
    probe_filters,
    text_entries,
    undeclared_dimensions,
)
from harvest.lifecycle import Adapter, Dropped, Kept, Run, Scope, Stored, Task
from retrieval.data_adapters.forsakringskassan import query_string


def report_of(key: str) -> str:
    """The report of a ``<language>/<report>/<Table>`` key."""
    return key.split("/", 2)[1]


def check_texts(body) -> dict:
    """Return the texts if they look like the text-key object, else raise."""
    if not isinstance(body, dict) or not any(
        isinstance(key, str) and key.startswith("MEKA.report.") for key in body
    ):
        raise ValueError(f"unexpected texts response {str(body)[:200]!r}")
    return body


async def fetch_table(
    client, report: Report, table: Table, listed: Listed
) -> tuple[dict | None, list[str]]:
    """Fetch one table's source responses: its catalogue row and, when
    ``meta.json`` lists a column it does not describe (such as ``kommun_kod``), a
    one-row probe showing whether its rows carry it. Such a table cannot be
    described completely and returns ``(None, undeclared keys)``.

    Raises:
        Unreachable: The probe request failed.
        ValueError: The probe was inconclusive.
    """
    responses = {"listing": listed.record()}
    if report.undeclared:
        url = f"{table_url(report.key, table.name)}?"
        rows = await client.get_json(url + query_string(probe_filters(report, table)))
        if extra := undeclared_dimensions(rows, table):
            return None, extra
        responses["probe"] = rows
    return responses, []


async def discover(run: Run) -> list[Scope]:
    problems: list = []
    reports_body = await run.client.get_json(REPORTS_URL)
    listed, complete = catalogue(reports_body, problems)
    if not listed:
        raise ValueError("Försäkringskassan lists no reports")
    for resource, error in problems:
        run.fail("discovery", f"reports/{resource}", error)
    run.store.put_shared(
        PROVIDER_CODE, "reports.json", reports_body, retrieved_at=run.stamp
    )
    try:
        texts = check_texts(await run.client.get_json(TEXTS_URL))
        run.store.put_shared(PROVIDER_CODE, "texts.json", texts, retrieved_at=run.stamp)
    except (Unreachable, ValueError) as error:
        texts = None  # reported once; nothing is refetched or verified this run
        run.fail("discovery", "texts", error)
    by_report = {item.report: item for item in listed}
    wanted = run.selection.get("subjects")
    if wanted is None:
        selected = listed
    else:
        selected = [
            by_report[name] for name in dict.fromkeys(wanted) if name in by_report
        ]
        for name in wanted:
            if name not in by_report:
                run.fail(
                    "discovery",
                    f"*/{name}",
                    LookupError("Unknown Försäkringskassan report"),
                )
    run.state.setdefault("reports", {})
    run.state["mapping_version"] = MAPPING_VERSION
    run.report["undeclared_dimensions"] = {}
    scopes = []
    for item in selected:
        scope = Scope(item.report, None)

        async def load(item=item, scope=scope) -> list[Task]:
            name = item.report
            meta = await run.client.get_json(meta_url(name))
            accepted = run.state["reports"].setdefault(name, {})
            try:
                report = parse_meta(meta, name, item.area)
                run.store.put_shared(
                    PROVIDER_CODE, meta_name(name), meta, retrieved_at=run.stamp
                )
            except Exception:
                accepted["complete"] = False
                raise
            tasks = [
                Task(f"{LANGUAGE}/{name}/{table_name}", ("duplicate", name))
                for table_name in report.duplicate_tables
            ]
            if texts is None:
                scope.complete = False  # tables neither rebuilt nor verified
                return tasks
            meta_sha256 = fingerprint(meta)
            texts_sha256 = fingerprint(text_entries(texts, report))
            excluded = accepted.get("undeclared") or {}
            tables = [t for t in report.tables if t.name not in excluded]
            unchanged = (
                not run.selection.get("full")
                and accepted.get("complete")
                and accepted.get("meta_sha256") == meta_sha256
                and accepted.get("texts_sha256") == texts_sha256
                and accepted.get("mapping_version") == MAPPING_VERSION
                and not report.duplicate_tables
                and all(
                    run.store.get(run.stored_key(f"{LANGUAGE}/{name}/{t.name}"))
                    is not None
                    for t in tables
                )
            )
            if unchanged:
                for table_name, keys in excluded.items():
                    run.report["undeclared_dimensions"][f"{name}/{table_name}"] = keys
                    run.counts["excluded"] += 1
                return tasks + [
                    Task(f"{LANGUAGE}/{name}/{t.name}", ("unchanged",)) for t in tables
                ]
            run.state["reports"][name] = {
                "meta_sha256": meta_sha256,
                "texts_sha256": texts_sha256,
                "mapping_version": MAPPING_VERSION,
                "tables": [t.name for t in report.tables],
                "undeclared": {},
                "complete": not report.duplicate_tables,
            }
            return tasks + [
                Task(f"{LANGUAGE}/{name}/{t.name}", ("fetch", report, t, item))
                for t in report.tables
            ]

        scope.load = load
        scopes.append(scope)
    return scopes


async def fetch(run: Run, task: Task) -> Stored | Kept | Dropped:
    kind = task.item[0]
    if kind == "unchanged":
        return Kept()
    if kind == "duplicate":  # quarantined: its last document is kept until stale
        raise ValueError("Table listed more than once")
    _, report, table, listed = task.item
    accepted = run.state["reports"][report.key]
    try:
        responses, extra = await fetch_table(run.client, report, table, listed)
    except Exception:
        accepted["complete"] = False
        raise
    if responses is None:
        accepted["undeclared"][table.name] = extra
        run.report["undeclared_dimensions"][f"{report.key}/{table.name}"] = extra
        return Dropped("excluded")
    return Stored(
        responses=responses,
        derived={"table": table_record(table)},
        info={"mapping_version": MAPPING_VERSION},
    )


def forget(run: Run, key: str) -> None:
    """Drop a report's state and meta.json once none of its tables remain."""
    report = report_of(key)
    if any(report_of(k) == report for k in run.known()):
        return
    run.state.get("reports", {}).pop(report, None)
    run.store.remove_shared(PROVIDER_CODE, meta_name(report))


ADAPTER = Adapter(
    name="forsakringskassan",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-forsakringskassan.yml",
    client=lambda binding: ForsakringskassanClient(),
    discover=discover,
    fetch=fetch,
    options={"subject": "subjects", "full": "full"},
    languages=(LANGUAGE,),
    counts=("excluded",),
    forget=forget,
    scope_of=report_of,
    scope_failures=lambda report: [
        e["resource"]
        for e in report.get("errors") or []
        if (
            str(e.get("resource")).startswith("*/")
            and "Unknown" not in str(e.get("error"))
        )
        or (e.get("stage") == "discovery" and e.get("resource") == "texts")
    ],
)

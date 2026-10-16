"""Weekly Försäkringskassan refresh with independent report and table failures.

Försäkringskassan publishes no modification date, ``ETag`` or ``Last-Modified``
(``meta.revision`` is 0 everywhere), so the change signal is a hash: of each
report's ``meta.json`` and of the text entries its documents use. Every run reads
``/reports``, the texts (once) and every selected report's ``meta.json``, storing
them as shared source files; an incremental run refetches only reports whose
hashes changed or whose last pass was incomplete, and a full run refetches every
report. Each table's envelope holds its catalogue row and, for tables with
undeclared columns, the one-row probe; unchanged hashes count ``unchanged``.

A report's ``meta.json`` is the endpoint check of its tables. A table is published
only while that endpoint works and its entry was verified (refetched, or confirmed
unchanged by its hashes) within ``max_age_days``; otherwise it is withheld in state
while its source files remain. When the texts fail, no entry is refetched or
verified and the failure is reported once as a dependency (``discovery``/``texts``).
Only a complete ``/reports`` listing in an unbounded run removes a report, and only
a parsed ``meta.json`` in an unbounded run removes one of its tables. Documents are
built later by ``parse``.
"""

import asyncio
import logging
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.forsakringskassan.api import (
    REPORTS_URL,
    TEXTS_URL,
    meta_url,
    table_url,
)
from apps.harvest.metadata_adapters.forsakringskassan.client import (
    ForsakringskassanClient,
    Unreachable,
)
from apps.harvest.metadata_adapters.forsakringskassan.mapping import (
    LANGUAGE,
    MAPPING_VERSION,
)
from apps.harvest.metadata_adapters.forsakringskassan.parse import (
    document_from,
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
from apps.harvest.metadata_adapters.forsakringskassan.state import (
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
from retrieval.data_adapters.forsakringskassan import query_string

logger = logging.getLogger("forsakringskassan_logger")

LANGUAGES = (LANGUAGE,)
COUNTS = (
    "rebuilt",
    "unchanged",
    "excluded",
    "failed",
    "withheld",
    "restored",
    "removed",
)


def report_of(key: str) -> str:
    """The report of a ``<language>/<report>/<Table>`` key."""
    return key.split("/", 2)[1]


def stored_key(key: str) -> str:
    """The source-store key of a ``<language>/<report>/<Table>`` state key."""
    language, code = key.split("/", 1)
    return source_key(PROVIDER_CODE, code, language)


def state_key(key: str) -> str:
    _, code, language = split_key(key)
    return f"{language}/{code}"


def _allowed_path(relative: str) -> bool:
    return relative in {STATE, REPORT} or relative.startswith("source/")


def _names(values, limit: int = 10) -> str:
    """A short, readable list of names for a log line."""
    values = list(values)
    shown = ", ".join(str(value) for value in values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def rebuild_reasons(
    full: bool,
    accepted: dict,
    meta_sha256: str,
    texts_sha256: str,
    duplicates,
    missing,
) -> list[str]:
    """Why an incremental run cannot skip a report, for the log only.

    Mirrors the skip condition in :func:`run`: every clause that fails is a reason.
    """
    reasons = []
    if full:
        reasons.append("full run")
    if not accepted:
        reasons.append("no accepted state yet")
    else:
        if not accepted.get("complete"):
            reasons.append("last build was incomplete")
        if accepted.get("meta_sha256") != meta_sha256:
            reasons.append("meta.json changed")
        if accepted.get("texts_sha256") != texts_sha256:
            reasons.append("texts changed")
        if accepted.get("mapping_version") != MAPPING_VERSION:
            previous = accepted.get("mapping_version")
            reasons.append(f"mapping version {previous} -> {MAPPING_VERSION}")
    if duplicates:
        reasons.append(f"duplicate tables {list(duplicates)}")
    if missing:
        reasons.append("tables without a stored source entry")
    return reasons


def check_texts(body: Any) -> dict:
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
        logger.debug(
            "Table %s/%s: meta.json lists undeclared columns %s; probing one row",
            report.key,
            table.name,
            list(report.undeclared),
        )
        url = f"{table_url(report.key, table.name)}?"
        rows = await client.get_json(url + query_string(probe_filters(report, table)))
        if extra := undeclared_dimensions(rows, table):
            logger.info(
                "Table %s/%s excluded: rows carry dimensions meta.json does not "
                "describe: %s",
                report.key,
                table.name,
                extra,
            )
            return None, extra
        logger.debug(
            "Table %s/%s: probe found no undeclared dimensions", report.key, table.name
        )
        responses["probe"] = rows
    return responses, []


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current report catalogue as ``{"report", "area", "group"}`` records."""
    connection = client or ForsakringskassanClient()
    try:
        logger.info("Discovering reports from %s", REPORTS_URL)
        listed, complete = catalogue(await connection.get_json(REPORTS_URL), [])
        logger.info(
            "Discovered %d reports (complete listing: %s)", len(listed), complete
        )
        return [item.record() for item in listed]
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client, texts: dict | None = None) -> list:
    """Build every table document of one catalogue record in memory (typed
    boundary only)."""
    listed = Listed(record["report"], record["area"], record["group"])
    texts = texts or check_texts(await client.get_json(TEXTS_URL))
    meta = await client.get_json(meta_url(listed.report))
    report = parse_meta(meta, listed.report, listed.area)
    logger.debug(
        "Fetching metadata of report %s: building %d tables",
        listed.report,
        len(report.tables),
    )
    documents = []
    for table in report.tables:
        responses, _ = await fetch_table(client, report, table, listed)
        if responses is not None:
            envelope = {
                "identifier": f"{listed.report}/{table.name}",
                "responses": responses,
                "derived": {"table": table_record(table)},
            }
            documents.append(document_from(envelope, report, meta, texts))
    logger.debug(
        "Report %s: %d of %d tables built",
        listed.report,
        len(documents),
        len(report.tables),
    )
    return documents


async def run(
    provider: dict,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    subjects=None,
    full: bool = False,
    languages=None,
) -> dict:
    """Refresh every (or each selected) report into a separate worker output.

    Args:
        subjects: Report keys (such as ``ase-avslutsanledning``) for a bounded run.
        full: Rebuild every table even when its report's hashes are unchanged.
    """
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(configured) - set(LANGUAGES) or set(selected_languages) - set(configured):
        raise ValueError(f"Försäkringskassan supports only Swedish: {configured}")
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not _allowed_path(relative) for relative in baseline):
        raise ValueError("Unexpected file in published Försäkringskassan data")
    state = load_state(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    store = SourceStore(output / "source", PROVIDER_CODE)
    # Placeholders stand in for documents: presence means a stored source entry.
    published, withheld = {}, {}
    for stored in store.entries():
        key = state_key(stored)
        status = state["datasets"].get(key, {}).get("status")
        (withheld if status == "withheld" else published)[key] = True
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    max_age = timedelta(days=provider.get("max_age_days", 60))
    cooldown = provider.get("retry_cooldown_seconds", 60)
    connection = client or ForsakringskassanClient()
    progress = Progress(PROVIDER_CODE, PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors: list[dict] = []
    wanted = list(dict.fromkeys(subjects)) if subjects else None
    undeclared: dict[str, list[str]] = {}
    tallies: Counter = Counter()
    reasons_seen: Counter = Counter()

    logger.info(
        "Run start: %s run, languages=%s, max_age_days=%s, "
        "retry_cooldown_seconds=%s, output=%s",
        "full (rebuild every selected report)"
        if full
        else "incremental (rebuild only changed or incomplete reports)",
        selected_languages,
        max_age.days,
        cooldown,
        output,
    )
    logger.info(
        "Subjects: %s",
        f"{len(wanted)} requested ({_names(wanted)})" if wanted else "all reports",
    )
    logger.info(
        "Published baseline: %d files, %d documents, %d withheld, %d tables with "
        "state, accepted state for %d reports",
        len(baseline),
        len(published),
        len(withheld),
        len(state["datasets"]),
        len(state["reports"]),
    )
    if state["mapping_version"] not in (None, MAPPING_VERSION):
        logger.info(
            "Mapping version changed from %s to %s: reports built with an older "
            "mapping are rebuilt",
            state["mapping_version"],
            MAPPING_VERSION,
        )

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        if resource.startswith(f"{LANGUAGE}/"):
            report, table = resource.split("/", 2)[1:]
            entry["url"] = table_url(report, table)
        elif resource.startswith("*/"):
            entry["url"] = meta_url(resource[2:])
        elif resource == "texts":
            entry["url"] = TEXTS_URL
        elif resource.startswith("reports"):
            entry["url"] = REPORTS_URL
        logger.error("Recorded %s failure for %s: %s", stage, resource, entry["error"])
        errors.append(entry)
        progress.error(stage, resource, LANGUAGE, entry["error"], url=entry.get("url"))
        return entry

    def known(report=None):
        keys = set(published) | set(withheld) | set(state["datasets"])
        return {
            key
            for key in keys
            if key.split("/", 1)[0] in selected_languages
            and (report is None or report_of(key) == report)
        }

    def remove(key):
        logger.debug("Removing %s from published, withheld and state", key)
        state["datasets"].pop(key, None)
        store.remove(stored_key(key))
        if (published.pop(key, None) or withheld.pop(key, None)) is not None:
            counts["removed"] += 1

    try:
        progress.update(phase="discovery", current="reports")
        logger.info("Discovery: reading the report catalogue from %s", REPORTS_URL)
        try:
            problems: list = []
            reports_body = await connection.get_json(REPORTS_URL)
            listed, complete = catalogue(reports_body, problems)
            if not listed:
                raise ValueError("Försäkringskassan lists no reports")
        except Exception as error:
            logger.error("Discovery of the report catalogue failed: %s", error)
            progress.error(
                "discovery", "reports", LANGUAGE, f"{error}", url=REPORTS_URL
            )
            raise
        logger.info(
            "Catalogue lists %d unique reports (complete listing: %s, %d malformed "
            "or duplicate entries)",
            len(listed),
            complete,
            len(problems),
        )
        for resource, error in problems:
            fail("discovery", f"reports/{resource}", error)
        store.put_shared(
            PROVIDER_CODE, "reports.json", reports_body, retrieved_at=stamp
        )
        by_report = {item.report: item for item in listed}

        progress.update(current="texts")
        texts: dict | None
        try:
            texts = check_texts(await connection.get_json(TEXTS_URL))
            logger.info("Texts loaded: %d entries", len(texts))
            store.put_shared(PROVIDER_CODE, "texts.json", texts, retrieved_at=stamp)
        except (Unreachable, ValueError) as error:
            texts = None
            logger.warning(
                "Texts unavailable: no document will be rebuilt or verified this "
                "run; existing documents stay as they are, unverified"
            )
            fail("discovery", "texts", error)

        if wanted is None:
            selected = listed
            logger.info(
                "Selection: all %d listed reports (unbounded run)", len(selected)
            )
            if complete:
                removed_before = counts["removed"]
                for key in sorted(k for k in known() if report_of(k) not in by_report):
                    remove(key)
                for report in sorted(set(state["reports"]) - set(by_report)):
                    logger.info("Report %s is no longer listed: state dropped", report)
                    state["reports"].pop(report)
                    store.remove_shared(PROVIDER_CODE, meta_name(report))
                if counts["removed"] > removed_before:
                    logger.info(
                        "Removed %d documents of reports no longer listed",
                        counts["removed"] - removed_before,
                    )
            else:
                logger.warning(
                    "Catalogue listing is incomplete: reports missing from it are "
                    "kept, not removed"
                )
        else:
            selected = [by_report[name] for name in wanted if name in by_report]
            logger.info(
                "Selection: %d of %d requested reports found among %d listed "
                "reports (%s); the others are left untouched and nothing is removed "
                "in a bounded run",
                len(selected),
                len(wanted),
                len(listed),
                _names(item.report for item in selected),
            )
            for name in wanted:
                if name not in by_report:
                    fail(
                        "discovery",
                        name,
                        LookupError("Unknown Försäkringskassan report"),
                    )

        working: dict[str, bool] = {}
        failures: dict[str, tuple[str, Exception]] = {}
        outcomes: dict[str, str] = {}
        settled: set[str] = set()

        def check(key):
            settled.add(key)
            info = state["datasets"].setdefault(key, {})
            info["checked_at"] = stamp
            return info

        async def harvest(item: Listed) -> bool:
            """Process one report; return whether an endpoint failure occurred."""
            name = item.report
            previous = known(name)
            for key in previous:
                check(key)
            logger.debug(
                "Report %s: reading meta.json (%d known tables)", name, len(previous)
            )
            try:
                meta = await connection.get_json(meta_url(name))
            except Unreachable as error:
                logger.warning(
                    "Report %s: meta.json unreachable (%s); its %d known tables "
                    "count as not working",
                    name,
                    error,
                    len(previous),
                )
                for key in previous:
                    working[key] = False
                failures[f"*/{name}"] = ("endpoint", error)
                return True
            failures.pop(f"*/{name}", None)
            for key in previous:
                working[key] = True
            try:
                report = parse_meta(meta, name, item.area)
            except Exception as error:
                logger.warning(
                    "Report %s: meta.json did not parse (%s: %s); previous documents "
                    "kept, report marked incomplete",
                    name,
                    type(error).__name__,
                    error,
                )
                failures[f"*/{name}"] = ("metadata", error)
                state["reports"].setdefault(name, {})["complete"] = False
                return False
            meta_sha256 = fingerprint(meta)
            try:
                store.put_shared(
                    PROVIDER_CODE, meta_name(name), meta, retrieved_at=stamp
                )
            except Rejected as error:
                failures[f"*/{name}"] = ("metadata", error)
                state["reports"].setdefault(name, {})["complete"] = False
                return False
            current = {f"{LANGUAGE}/{name}/{t.name}": t for t in report.tables}
            for table_name in report.duplicate_tables:  # quarantined
                logger.warning(
                    "Report %s: table %s is listed more than once; quarantined, its "
                    "last document is kept until stale",
                    name,
                    table_name,
                )
                key = f"{LANGUAGE}/{name}/{table_name}"
                current[key] = None
                check(key)
                working[key] = True  # keeps its last document until it is stale
                failures[key] = ("metadata", ValueError("Table listed more than once"))
            vanished = sorted(previous - set(current))
            if wanted is None:
                for key in vanished:
                    settled.discard(key)
                    failures.pop(key, None)
                    remove(key)
                if vanished:
                    logger.info(
                        "Report %s: %d table(s) no longer in meta.json removed (%s)",
                        name,
                        len(vanished),
                        _names(vanished, 5),
                    )
            elif vanished:
                logger.info(
                    "Report %s: %d table(s) missing from meta.json kept (bounded run)",
                    name,
                    len(vanished),
                )
            if texts is None:  # reported once as the texts dependency failure:
                logger.debug(
                    "Report %s: texts unavailable, tables neither rebuilt nor verified",
                    name,
                )
                return False  # previous documents stay as they are, unverified
            texts_sha256 = fingerprint(text_entries(texts, report))
            accepted = state["reports"].get(name) or {}
            excluded = accepted.get("undeclared") or {}
            tables = [t for t in report.tables if t.name not in excluded]
            if (
                not full
                and accepted.get("complete")
                and accepted.get("meta_sha256") == meta_sha256
                and accepted.get("texts_sha256") == texts_sha256
                and accepted.get("mapping_version") == MAPPING_VERSION
                and not report.duplicate_tables
                and all(
                    store.get(stored_key(f"{LANGUAGE}/{name}/{t.name}")) is not None
                    for t in tables
                )
            ):
                tallies["unchanged"] += 1
                logger.debug(
                    "Report %s: unchanged (meta.json, texts and mapping version "
                    "match the accepted build): skipped, %d table(s) verified, %d "
                    "excluded",
                    name,
                    len(tables),
                    len(excluded),
                )
                for table in tables:
                    key = f"{LANGUAGE}/{name}/{table.name}"
                    check(key).update(verified_at=stamp, error=None)
                    working[key] = True
                    failures.pop(key, None)
                    outcomes[key] = "unchanged"
                for table_name, keys in excluded.items():
                    undeclared[f"{name}/{table_name}"] = keys
                    outcomes[f"{LANGUAGE}/{name}/{table_name}"] = "excluded"
                return False

            reasons = rebuild_reasons(
                full,
                accepted,
                meta_sha256,
                texts_sha256,
                report.duplicate_tables,
                [
                    t.name
                    for t in tables
                    if store.get(stored_key(f"{LANGUAGE}/{name}/{t.name}")) is None
                ],
            )
            tallies["rebuilt"] += 1
            reasons_seen.update(reasons)
            logger.info(
                "Report %s: rebuilding %d table(s) (%s)",
                name,
                len(report.tables),
                "; ".join(reasons),
            )
            endpoint_failed, complete_build, found = False, True, {}
            built: Counter = Counter()
            for table in report.tables:
                key = f"{LANGUAGE}/{name}/{table.name}"
                progress.update(current=key)
                info = check(key)
                try:
                    responses, extra = await fetch_table(
                        connection, report, table, item
                    )
                except Unreachable as error:
                    logger.warning(
                        "Table %s: probe request failed (%s); endpoint counts as "
                        "not working",
                        key,
                        error,
                    )
                    working[key], failures[key] = False, ("endpoint", error)
                    endpoint_failed, complete_build = True, False
                    built["failed"] += 1
                    continue
                except Exception as error:  # meta.json answered: endpoint works
                    logger.warning(
                        "Table %s: probe inconclusive (%s: %s); endpoint works, "
                        "stored entry kept",
                        key,
                        type(error).__name__,
                        error,
                    )
                    working[key], failures[key] = True, ("metadata", error)
                    complete_build = False
                    built["failed"] += 1
                    continue
                failures.pop(key, None)
                working[key] = True
                if responses is None:
                    found[table.name] = extra
                    undeclared[f"{name}/{table.name}"] = extra
                    settled.discard(key)
                    outcomes[key] = "excluded"
                    built["excluded"] += 1
                    remove(key)
                    continue
                stored = store.get(stored_key(key))
                try:
                    store.put(
                        stored_key(key),
                        responses=responses,
                        derived={"table": table_record(table)},
                        updated=None,
                        retrieved_at=stamp,
                    )
                except Rejected as error:
                    working[key], failures[key] = True, ("metadata", error)
                    complete_build = False
                    built["failed"] += 1
                    continue
                signals_unchanged = (
                    accepted.get("meta_sha256") == meta_sha256
                    and accepted.get("texts_sha256") == texts_sha256
                    and accepted.get("mapping_version") == MAPPING_VERSION
                )
                outcomes[key] = (
                    "unchanged"
                    if signals_unchanged
                    and stored is not None
                    and stored["responses"] == responses
                    else "rebuilt"
                )
                built[outcomes[key]] += 1
                logger.debug("Table %s: %s", key, outcomes[key])
                info.update(
                    verified_at=stamp, mapping_version=MAPPING_VERSION, error=None
                )
                (withheld if key in withheld else published)[key] = True
            state["reports"][name] = {
                "meta_sha256": meta_sha256,
                "texts_sha256": texts_sha256,
                "mapping_version": MAPPING_VERSION,
                "tables": [t.name for t in report.tables],
                "undeclared": found,
                "complete": complete_build and not report.duplicate_tables,
            }
            logger.info(
                "Report %s: %d rebuilt, %d unchanged, %d excluded, %d failed "
                "(complete: %s)",
                name,
                built["rebuilt"],
                built["unchanged"],
                built["excluded"],
                built["failed"],
                state["reports"][name]["complete"],
            )
            return endpoint_failed

        progress.update(phase="metadata", total=len(selected), processed=0)
        logger.info(
            "Metadata: checking %d reports one at a time; a report is skipped when "
            "its meta.json and text hashes and the mapping version match its last "
            "complete build%s",
            len(selected),
            " (disabled by the full run)" if full else "",
        )
        retry = []
        for number, item in enumerate(selected, start=1):
            progress.update(current=item.report, operation="refresh")
            if await harvest(item):
                retry.append(item)
            progress.update(
                processed=number,
                succeeded=sum(o != "excluded" for o in outcomes.values()),
            )
            if number % 25 == 0 or number == len(selected):
                logger.info(
                    "Processed %d/%d reports: %d skipped as unchanged, %d rebuilt, "
                    "%d with endpoint failures so far",
                    number,
                    len(selected),
                    tallies["unchanged"],
                    tallies["rebuilt"],
                    len(retry),
                )
        if reasons_seen:
            logger.info(
                "Rebuild reasons: %s",
                ", ".join(f"{reason} x{n}" for reason, n in reasons_seen.most_common()),
            )
        if retry:
            progress.update(operation="retry cooldown", current=None)
            logger.info(
                "Endpoint failures in %d report(s) (%s): cooling down %ss, then "
                "retrying once",
                len(retry),
                _names(item.report for item in retry),
                cooldown,
            )
            await asyncio.sleep(cooldown)
            still_failing = 0
            for item in retry:
                progress.update(current=item.report, operation="retry")
                if await harvest(item):
                    still_failing += 1
            logger.info(
                "Retry pass finished: %d of %d report(s) still fail",
                still_failing,
                len(retry),
            )
        for outcome in outcomes.values():
            counts[outcome] += 1
        logger.info(
            "Table outcomes: %d rebuilt, %d unchanged, %d excluded; %d failure(s) "
            "to record",
            counts["rebuilt"],
            counts["unchanged"],
            counts["excluded"],
            len(failures),
        )
        for key, (stage, error) in sorted(failures.items()):
            entry = fail(stage, key, error)
            counts["failed"] += 1
            for affected in known(key[2:]) if key.startswith("*/") else [key]:
                if affected in state["datasets"]:
                    state["datasets"][affected]["error"] = entry
        progress.update(failed=counts["failed"])
        withheld_before = set(withheld)
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
        newly_withheld = sorted(set(withheld) - withheld_before)
        for key in newly_withheld:
            logger.debug(
                "Table %s withheld: %s",
                key,
                state["datasets"][key].get("withheld_reason"),
            )
        withheld_why = Counter(
            state["datasets"].get(key, {}).get("withheld_reason") for key in withheld
        )
        logger.info(
            "Settlement (max age %d days): %d published, %d withheld (%d newly "
            "withheld, %d restored)%s",
            max_age.days,
            len(published),
            len(withheld),
            counts["withheld"],
            counts["restored"],
            "; withheld because "
            + ", ".join(f"{r}: {n}" for r, n in withheld_why.items())
            if withheld
            else "",
        )
    finally:
        if client is None:
            await connection.close()

    state["mapping_version"] = MAPPING_VERSION
    store.save()
    save(output, state)
    status = report_status(pending=0, errors=errors)
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "selection": {
            "subjects": wanted,
            "full": bool(full),
            "languages": selected_languages,
        },
        "counts": counts,
        "requests": getattr(connection, "request_count", None),
        "published": {
            language: sum(k.startswith(f"{language}/") for k in published)
            for language in configured
        },
        "withheld": sorted(withheld),
        "undeclared_dimensions": dict(sorted(undeclared.items())),
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    logger.info(
        "Run finished: status=%s, requests=%s, counts=%s, published=%s, withheld=%d, "
        "undeclared-dimension tables=%d, errors=%d",
        status,
        result["requests"],
        counts,
        result["published"],
        len(withheld),
        len(undeclared),
        len(errors),
    )
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting artifact %s (run status %s) into %s", output, result["status"], data
    )
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        logger.error("Artifact rejected: the published baseline changed since the run")
        raise ValueError("Stale Försäkringskassan artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        logger.error("Artifact rejected: checksum mismatch")
        raise ValueError("Försäkringskassan artifact checksum mismatch")
    if not {STATE, REPORT} <= set(result["manifest"]) or not all(
        _allowed_path(relative) for relative in result["manifest"]
    ):
        logger.error(
            "Artifact rejected: unexpected paths %s", sorted(result["manifest"])
        )
        raise ValueError("Unexpected Försäkringskassan artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    for relative in harvest_manifest(data):
        if relative.startswith("source/") and relative not in result["manifest"]:
            (data / relative).unlink()
    logger.info("Collected %d files into %s", len(result["manifest"]), data)
    return result

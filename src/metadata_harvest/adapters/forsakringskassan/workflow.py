"""Weekly Försäkringskassan refresh with independent report and table failures.

Försäkringskassan publishes no modification date, ``ETag`` or ``Last-Modified``
(``meta.revision`` is 0 everywhere), so the change signal is a hash: of each
report's ``meta.json`` and of the text entries its documents use. Every run reads
``/reports``, the texts (once) and every selected report's ``meta.json``; an
incremental run rebuilds only reports whose hashes changed or whose last build was
incomplete, and a full run rebuilds every report. An identical rebuild keeps its
accepted bytes and counts ``unchanged``.

A report's ``meta.json`` is the endpoint check of its tables. A table is published
only while that endpoint works and its document was verified (rebuilt, or confirmed
unchanged by its hashes) within ``max_age_days``; otherwise its last good document
is withheld in state. When the texts fail, no document is rebuilt or verified and
the failure is reported once as a dependency (``discovery``/``texts``). Only a
complete ``/reports`` listing in an unbounded run removes a report, and only a
parsed ``meta.json`` in an unbounded run removes one of its tables.
"""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from metadata_harvest.adapters.forsakringskassan.api import (
    REPORTS_URL,
    TEXTS_URL,
    meta_url,
    table_url,
)
from metadata_harvest.adapters.forsakringskassan.client import (
    ForsakringskassanClient,
    Unreachable,
)
from metadata_harvest.adapters.forsakringskassan.mapping import (
    LANGUAGE,
    MAPPING_VERSION,
    build_dataset,
)
from metadata_harvest.adapters.forsakringskassan.retrieval import query_string
from metadata_harvest.adapters.forsakringskassan.source import (
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
from metadata_harvest.adapters.forsakringskassan.state import (
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


def check_texts(body: Any) -> dict:
    """Return the texts if they look like the text-key object, else raise."""
    if not isinstance(body, dict) or not any(
        isinstance(key, str) and key.startswith("MEKA.report.") for key in body
    ):
        raise ValueError(f"unexpected texts response {str(body)[:200]!r}")
    return body


async def build_table(
    client,
    report: Report,
    table: Table,
    listed: Listed,
    texts: dict,
    *,
    meta_sha256: str,
    texts_sha256: str,
) -> tuple[dict | None, list[str]]:
    """Build and validate one table's document.

    When ``meta.json`` lists a column it does not describe (such as ``kommun_kod``),
    a one-row probe of the table shows whether its rows carry it. Such a table
    cannot be described completely and returns ``(None, undeclared keys)``.

    Raises:
        Unreachable: The probe request failed.
        ValueError: The probe was inconclusive or the document is invalid.
    """
    if report.undeclared:
        url = f"{table_url(report.key, table.name)}?"
        rows = await client.get_json(url + query_string(probe_filters(report, table)))
        if extra := undeclared_dimensions(rows, table):
            return None, extra
    document = build_dataset(
        report,
        table,
        listed,
        texts,
        meta_sha256=meta_sha256,
        texts_sha256=texts_sha256,
    )
    validate_metadata(document, adapter=PROVIDER_CODE)
    return document, []


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current report catalogue as ``{"report", "area", "group"}`` records."""
    connection = client or ForsakringskassanClient()
    try:
        listed, _ = catalogue(await connection.get_json(REPORTS_URL), [])
        return [item.record() for item in listed]
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client, texts: dict | None = None) -> list:
    """Build and validate every table document of one catalogue record."""
    listed = Listed(record["report"], record["area"], record["group"])
    texts = texts or check_texts(await client.get_json(TEXTS_URL))
    meta = await client.get_json(meta_url(listed.report))
    report = parse_meta(meta, listed.report, listed.area)
    documents = []
    for table in report.tables:
        document, _ = await build_table(
            client,
            report,
            table,
            listed,
            texts,
            meta_sha256=fingerprint(meta),
            texts_sha256=fingerprint(text_entries(texts, report)),
        )
        if document is not None:
            documents.append(document)
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
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published Försäkringskassan data")
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
    connection = client or ForsakringskassanClient()
    progress = Progress(PROVIDER_CODE, PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors: list[dict] = []
    wanted = list(dict.fromkeys(subjects)) if subjects else None
    undeclared: dict[str, list[str]] = {}

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
        errors.append(entry)
        progress.error(stage, resource, LANGUAGE, entry["error"])
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
        state["datasets"].pop(key, None)
        if (published.pop(key, None) or withheld.pop(key, None)) is not None:
            counts["removed"] += 1

    try:
        progress.update(phase="discovery", current="reports")
        try:
            problems: list = []
            listed, complete = catalogue(
                await connection.get_json(REPORTS_URL), problems
            )
            if not listed:
                raise ValueError("Försäkringskassan lists no reports")
        except Exception as error:
            progress.error("discovery", "reports", LANGUAGE, f"{error}")
            raise
        for resource, error in problems:
            fail("discovery", f"reports/{resource}", error)
        by_report = {item.report: item for item in listed}

        progress.update(current="texts")
        texts: dict | None
        try:
            texts = check_texts(await connection.get_json(TEXTS_URL))
        except (Unreachable, ValueError) as error:
            texts = None
            fail("discovery", "texts", error)

        if wanted is None:
            selected = listed
            if complete:
                for key in sorted(k for k in known() if report_of(k) not in by_report):
                    remove(key)
                for report in sorted(set(state["reports"]) - set(by_report)):
                    state["reports"].pop(report)
        else:
            selected = [by_report[name] for name in wanted if name in by_report]
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
            try:
                meta = await connection.get_json(meta_url(name))
            except Unreachable as error:
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
                failures[f"*/{name}"] = ("metadata", error)
                state["reports"].setdefault(name, {})["complete"] = False
                return False
            meta_sha256 = fingerprint(meta)
            current = {f"{LANGUAGE}/{name}/{t.name}": t for t in report.tables}
            for table_name in report.duplicate_tables:  # quarantined
                key = f"{LANGUAGE}/{name}/{table_name}"
                current[key] = None
                check(key)
                working[key] = True  # keeps its last document until it is stale
                failures[key] = ("metadata", ValueError("Table listed more than once"))
            if wanted is None:
                for key in sorted(previous - set(current)):
                    settled.discard(key)
                    failures.pop(key, None)
                    remove(key)
            if texts is None:  # reported once as the texts dependency failure:
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
                    f"{LANGUAGE}/{name}/{t.name}" in published
                    or f"{LANGUAGE}/{name}/{t.name}" in withheld
                    for t in tables
                )
            ):
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

            endpoint_failed, complete_build, found = False, True, {}
            for table in report.tables:
                key = f"{LANGUAGE}/{name}/{table.name}"
                progress.update(current=key)
                info = check(key)
                try:
                    document, extra = await build_table(
                        connection,
                        report,
                        table,
                        item,
                        texts,
                        meta_sha256=meta_sha256,
                        texts_sha256=texts_sha256,
                    )
                except Unreachable as error:
                    working[key], failures[key] = False, ("endpoint", error)
                    endpoint_failed, complete_build = True, False
                    continue
                except Exception as error:  # meta.json answered: endpoint works
                    working[key], failures[key] = True, ("metadata", error)
                    complete_build = False
                    continue
                failures.pop(key, None)
                working[key] = True
                if document is None:
                    found[table.name] = extra
                    undeclared[f"{name}/{table.name}"] = extra
                    settled.discard(key)
                    outcomes[key] = "excluded"
                    remove(key)
                    continue
                previous_document = published.get(key) or withheld.get(key)
                outcomes[key] = (
                    "unchanged" if document == previous_document else "rebuilt"
                )
                info.update(
                    verified_at=stamp, mapping_version=MAPPING_VERSION, error=None
                )
                (withheld if key in withheld else published)[key] = document
            state["reports"][name] = {
                "meta_sha256": meta_sha256,
                "texts_sha256": texts_sha256,
                "mapping_version": MAPPING_VERSION,
                "tables": [t.name for t in report.tables],
                "undeclared": found,
                "complete": complete_build and not report.duplicate_tables,
            }
            return endpoint_failed

        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, item in enumerate(selected, start=1):
            progress.update(current=item.report, operation="refresh")
            if await harvest(item):
                retry.append(item)
            progress.update(
                processed=number,
                succeeded=sum(o != "excluded" for o in outcomes.values()),
            )
        if retry:
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for item in retry:
                progress.update(current=item.report, operation="retry")
                await harvest(item)
        for outcome in outcomes.values():
            counts[outcome] += 1
        for key, (stage, error) in sorted(failures.items()):
            entry = fail(stage, key, error)
            counts["failed"] += 1
            for affected in known(key[2:]) if key.startswith("*/") else [key]:
                if affected in state["datasets"]:
                    state["datasets"][affected]["error"] = entry
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

    state["mapping_version"] = MAPPING_VERSION
    save(output, state, published, configured)
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
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale Försäkringskassan artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Försäkringskassan artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        raise ValueError("Unexpected Försäkringskassan artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    return result

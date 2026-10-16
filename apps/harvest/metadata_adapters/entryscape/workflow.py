"""Monthly, incremental RowStore refresh with a hard request budget.

EntryScape hosts rate-limit us, so a run spends requests only where the
catalogue shows change: a table whose distribution and dataset ``modified``
markers equal those of its last harvest costs nothing; a table without markers
costs one ``_limit=1`` request to compare its row count; new, changed and
previously failed tables are harvested (source files first, then enumeration,
then a full scan, deferred beyond ``max_paging_requests``). One pacer spaces
every request across all hosts; the second HTTP 429 or an exhausted budget
stops the run, keeping every finished table for the next one.

Each harvested table's envelope holds its discovery record and, as derived data,
the trimmed structure document the dataset rules read (the rows themselves are
never stored). Dataset grouping and mapping happen later in ``parse``, so nothing
is requested to apply a rule change.
"""

import logging
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from apps.harvest.metadata_adapters.entryscape.categories import (
    EmptyTable,
    NeedsPaging,
    infer_structure,
)
from apps.harvest.metadata_adapters.entryscape.client import (
    BudgetExhausted,
    Client,
    Pacer,
    RateLimited,
)
from apps.harvest.metadata_adapters.entryscape.datasets import MAPPING_VERSION, trim
from apps.harvest.metadata_adapters.entryscape.discovery import (
    discover_provider as discover,
)
from apps.harvest.metadata_adapters.entryscape.parse import stored_key
from apps.harvest.metadata_adapters.entryscape.state import (
    CURRENT,
    PROVIDER_CODE,
    REPORT,
    TABLES,
    load_current,
    load_tables,
    save_current,
    save_tables,
    table_key,
)
from apps.harvest.metadata_adapters.entryscape.tables import table_document
from apps.harvest.source_store import SourceStore, harvest_manifest
from apps.harvest.storage import read, write
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest
from registry import ServiceBinding

logger = logging.getLogger("entryscape_logger")

REQUESTS_PER_SECOND = 1.0
DEFAULT_MAX_REQUESTS = 1500
MAX_THROTTLES = 1
MAX_PAGING_REQUESTS = 10
MAX_TABLE_ERRORS = 3
"""Unexpected table failures tolerated per run; a bug must not repeat 500 times."""
PREFLIGHT_HOSTS = (
    "https://skatteverket.entryscape.net",
    "https://catalog.goteborg.se",
    "https://catalog.ale.se",
)
CONNECTION_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
COUNTS = (
    "unchanged",
    "harvested",
    "empty",
    "deferred",
    "removed",
    "failed",
)


def _allowed_path(relative: str) -> bool:
    return relative in {TABLES, CURRENT, REPORT} or relative.startswith("source/")


class Blocked(RuntimeError):
    """A host refused connections, throttled us twice or the budget ran out."""


def unresolvable(error: object) -> bool:
    text = str(error)
    return any(
        sign in text
        for sign in ("Name or service not known", "getaddrinfo failed", "nodename")
    )


def markers(record: dict) -> dict:
    catalogue = record.get("catalogue") or {}
    return {
        "distribution_modified": catalogue.get("distribution_modified"),
        "dataset_modified": catalogue.get("dataset_modified"),
    }


def default_client(base_url: str, pacer: Pacer) -> Client:
    return Client(base_url, pacer=pacer)


def tables_key(key: str) -> dict:
    """A record-like identity from a ``<provider>/<dataset_code>`` table key."""
    provider_code, dataset_code = key.split("/", 1)
    return {"provider_code": provider_code, "dataset_code": dataset_code}


def _discovery(store: SourceStore, entry: dict) -> dict:
    envelope = store.get(stored_key(entry))
    return envelope["responses"]["discovery"] if envelope else {}


def publish(tables: dict, current: dict, store: SourceStore, output: Path) -> None:
    """Write the source files and state into ``output``."""
    store.save()
    logger.info(
        "Wrote %d stored tables and state for %d known tables to %s",
        len(store.entries()),
        len(tables),
        output,
    )
    current.update(mapping_version=MAPPING_VERSION)
    save_tables(output, tables)
    save_current(output, current)


async def discover_provider(binding: ServiceBinding, *, client=None) -> dict:
    """The provider's single-language discovery scope."""
    logger.debug("Discovering provider %s on its own", binding.source_code)
    connection = client or Client(binding.endpoint_url)
    try:
        async for scope in discover(binding, client=connection):
            return scope
    finally:
        if client is None:
            await connection.http.aclose()
    raise ValueError("Discovery yielded no scope")


async def fetch_metadata(record: dict, *, client) -> dict:
    """The trimmed structure document of one discovered table."""
    logger.debug("Fetching structure of table %s", record["table_url"])
    structure = await infer_structure(
        client,
        record["table_url"],
        source_uri=(record.get("catalogue") or {}).get("source_distribution"),
    )
    return trim(table_document(record, structure))


async def run(
    bindings: list[ServiceBinding],
    data: Path,
    output: Path,
    *,
    client_factory=None,
    clock=None,
    selected=None,
    max_requests=DEFAULT_MAX_REQUESTS,
    max_paging_requests=MAX_PAGING_REQUESTS,
    requests_per_second=REQUESTS_PER_SECOND,
    preflight=True,
) -> dict:
    """Refresh changed tables of every (or each selected) provider."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not _allowed_path(relative) for relative in baseline):
        raise ValueError("Unexpected file in published EntryScape data")
    tables, current = load_tables(data), load_current(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    store = SourceStore(output / "source", PROVIDER_CODE)
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    factory = client_factory or default_client
    pacer = Pacer(
        1 / requests_per_second, budget=max_requests, max_throttles=MAX_THROTTLES
    )
    progress = Progress("entryscape", "entryscape")
    counts = dict.fromkeys(COUNTS, 0)
    errors, deferred, requests = [], [], 0
    interrupted = None
    strategies = Counter()
    done = 0
    wanted = set(selected) if selected else None
    chosen = [b for b in bindings if wanted is None or b.source_code in wanted]
    for code in sorted((wanted or set()) - {b.source_code for b in bindings}):
        logger.warning("Requested provider %r is not configured; skipping it", code)
        errors.append(
            {"stage": "discovery", "resource": code, "error": "Unknown provider"}
        )
    logger.info(
        "EntryScape run starting: %d of %d configured providers (%s), "
        "max_requests=%d, max_paging_requests=%d, requests_per_second=%.2f, "
        "preflight=%s",
        len(chosen),
        len(bindings),
        ", ".join(sorted(wanted)) if wanted else "all",
        max_requests,
        max_paging_requests,
        requests_per_second,
        preflight,
    )
    if tables:
        logger.info(
            "Incremental refresh: %d tables known from earlier runs (%s); "
            "unchanged tables will be skipped",
            len(tables),
            ", ".join(
                f"{n} {s}"
                for s, n in sorted(
                    Counter(t.get("status") for t in tables.values()).items()
                )
            ),
        )
    else:
        logger.info("Initial harvest: no accepted tables yet, every table is new")
    if wanted is not None:
        logger.info(
            "Provider selection active: tables of unselected providers are left "
            "untouched and absent tables are not pruned"
        )
    if current.get("mapping_version") not in (None, MAPPING_VERSION):
        logger.info(
            "Mapping version changed (%s -> %s); datasets are regrouped by parse "
            "from stored tables without any request",
            current.get("mapping_version"),
            MAPPING_VERSION,
        )

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        if stage != "run":
            logger.error("%s failed for %s: %s", stage, resource, entry["error"])
        progress.error(stage, resource, "sv", entry["error"])
        return entry

    async def call(base_url, work):
        nonlocal requests
        async with factory(base_url, pacer) as client:
            try:
                return await work(client)
            except CONNECTION_ERRORS as error:
                if unresolvable(error):
                    raise
                logger.error("Cannot connect to %s: %s", base_url, type(error).__name__)
                raise Blocked(f"{base_url}: {type(error).__name__}") from error
            except (RateLimited, BudgetExhausted) as error:
                raise Blocked(f"{base_url}: {error}") from error
            finally:
                requests += client.requests

    async def harvest(record: dict) -> str:
        key = table_key(record)
        entry = {
            "key": key,
            "provider_code": record["provider_code"],
            "dataset_code": record["dataset_code"],
            "markers": markers(record),
            "harvested_at": stamp,
        }
        try:
            structure = await call(
                record["base_url"],
                lambda client: infer_structure(
                    client,
                    record["table_url"],
                    source_uri=(record.get("catalogue") or {}).get(
                        "source_distribution"
                    ),
                    max_paging_requests=max_paging_requests,
                ),
            )
        except EmptyTable:
            entry.update(status="empty", rowcount=0)
            store.remove(stored_key(record))
            counts["empty"] += 1
            logger.debug("%s: empty table (0 rows), recorded", key)
        except NeedsPaging as deferral:
            entry.update(
                status="deferred",
                rowcount=deferral.rowcount,
                reason=deferral.reason,
                paging_requests=deferral.estimated_requests,
            )
            deferred.append(
                {
                    "table": key,
                    **{k: entry[k] for k in ("rowcount", "reason", "paging_requests")},
                }
            )
            counts["deferred"] += 1
            logger.debug(
                "%s: deferred, %d rows need ~%d paging requests (limit %d): %s",
                key,
                deferral.rowcount,
                deferral.estimated_requests,
                max_paging_requests,
                deferral.reason,
            )
        else:
            store.put(
                stored_key(record),
                responses={"discovery": record},
                derived={"document": trim(table_document(record, structure))},
                updated=record.get("published"),
                retrieved_at=stamp,
            )
            entry.update(
                status="ok",
                rowcount=structure["rowcount"],
                strategy=structure["strategy"],
                requests=structure["requests"],
            )
            counts["harvested"] += 1
            strategies[structure["strategy"]] += 1
            logger.debug(
                "%s: harvested %d rows via %s in %d requests",
                key,
                structure["rowcount"],
                structure["strategy"],
                structure["requests"],
            )
        tables[key] = entry
        return entry["status"]

    try:
        if preflight:
            progress.update(phase="discovery", current="preflight")
            logger.info("Preflight: checking %d hosts", len(PREFLIGHT_HOSTS))
            for host in PREFLIGHT_HOSTS:
                try:
                    await call(
                        host,
                        lambda client: client.get(
                            "store/search",
                            {"type": "solr", "query": "rdfType:x", "limit": 1},
                        ),
                    )
                except Exception as error:
                    logger.error(
                        "Preflight failed for %s (%s); not starting the run",
                        host,
                        type(error).__name__,
                    )
                    raise Blocked(f"{host} is not reachable; not starting") from error
                logger.debug("Preflight: %s reachable", host)
        else:
            logger.info("Preflight skipped")
        table_errors = 0
        progress.update(phase="metadata", total=len(chosen), processed=0)
        for position, binding in enumerate(chosen, start=1):
            code = binding.source_code
            progress.update(current=code, operation="discovery")
            logger.info(
                "[%d/%d] Provider %s: discovering tables (%d/%d requests spent)",
                position,
                len(chosen),
                code,
                pacer.sent,
                max_requests,
            )
            before = dict(counts)
            decisions = Counter()
            try:
                scope = await call(
                    binding.endpoint_url,
                    lambda client, binding=binding: discover_provider(
                        binding, client=client
                    ),
                )
            except Blocked:
                raise
            except Exception as error:
                fail("discovery", code, error)
                logger.warning(
                    "Provider %s skipped: discovery failed, its %d known tables "
                    "are kept as they are",
                    code,
                    sum(1 for k in tables if k.startswith(code + "/")),
                )
                current["providers"][code] = {"checked_at": stamp, "complete": False}
                continue
            if scope["errors"]:
                for text in scope["errors"]:
                    fail("discovery", code, RuntimeError(text))
                if any("Connect" in e and not unresolvable(e) for e in scope["errors"]):
                    raise Blocked(f"{binding.endpoint_url}: {scope['errors']}")
            current["providers"][code] = {
                "checked_at": stamp,
                "complete": scope["complete"],
                "tables": len(scope["records"]),
            }
            listed = {table_key(r): r for r in scope["records"]}
            known = sum(1 for k in tables if k.startswith(code + "/"))
            logger.info(
                "Provider %s: %d tables listed, %d already known, %d new; "
                "scope complete=%s",
                code,
                len(listed),
                len(listed.keys() & tables.keys()),
                len(listed.keys() - tables.keys()),
                scope["complete"],
            )
            if scope["complete"] and wanted is None:
                for key in [
                    k for k in tables if k.startswith(code + "/") and k not in listed
                ]:
                    tables.pop(key)
                    store.remove(stored_key(tables_key(key)))
                    counts["removed"] += 1
                    logger.debug("%s: no longer listed by the provider; removed", key)
                if counts["removed"] > before["removed"]:
                    logger.info(
                        "Provider %s: %d of %d known tables are no longer listed "
                        "and were removed",
                        code,
                        counts["removed"] - before["removed"],
                        known,
                    )
            elif not scope["complete"]:
                logger.warning(
                    "Provider %s: scope incomplete, not pruning its %d known tables",
                    code,
                    known,
                )
            else:
                logger.info(
                    "Provider %s: selection active, not pruning absent tables", code
                )
            for key, record in listed.items():
                previous = tables.get(key)
                settled = previous is not None and previous.get("status") in (
                    "ok",
                    "empty",
                    "deferred",
                )
                if (
                    settled
                    and previous["status"] == "ok"
                    and (store.get(stored_key(record)) is None)
                ):
                    settled = False  # harvested before the source files existed
                why = (
                    "new table"
                    if previous is None
                    else f"previous attempt ended as {previous.get('status')}"
                )
                if settled and any(markers(record).values()):
                    if previous["markers"] == markers(record):
                        counts["unchanged"] += 1
                        decisions["unchanged (modified markers equal)"] += 1
                        logger.debug(
                            "%s: unchanged (%s), skipped", key, previous.get("status")
                        )
                        continue
                    why = "modified markers changed"
                    logger.debug(
                        "%s: markers changed %s -> %s",
                        key,
                        previous["markers"],
                        markers(record),
                    )
                elif settled:
                    logger.debug(
                        "%s: no modified markers in the catalogue; probing row count",
                        key,
                    )
                    progress.update(current=key, operation="row count")
                    page = await call(
                        record["base_url"],
                        lambda client, record=record: client.get(
                            record["table_url"], {"_limit": 1}
                        ),
                    )
                    if page.get("resultCount") == previous.get("rowcount"):
                        previous["markers"] = markers(record)
                        counts["unchanged"] += 1
                        decisions["unchanged (row count equal)"] += 1
                        logger.debug(
                            "%s: row count still %s, skipped",
                            key,
                            previous.get("rowcount"),
                        )
                        continue
                    why = "row count changed"
                    logger.debug(
                        "%s: row count %s -> %s",
                        key,
                        previous.get("rowcount"),
                        page.get("resultCount"),
                    )
                decisions[f"harvest: {why}"] += 1
                logger.debug("%s: harvesting (%s)", key, why)
                progress.update(current=key, operation="harvest")
                try:
                    await harvest(record)
                except Blocked:
                    raise
                except Exception as error:
                    tables[key] = {
                        "key": key,
                        "provider_code": record["provider_code"],
                        "dataset_code": record["dataset_code"],
                        "markers": markers(record),
                        "harvested_at": stamp,
                        "status": "error",
                        "error": fail("metadata", key, error),
                    }
                    counts["failed"] += 1
                    table_errors += 1
                    logger.warning(
                        "%s: failure recorded (%d/%d tolerated); the stored source "
                        "entry is kept",
                        key,
                        table_errors,
                        MAX_TABLE_ERRORS,
                    )
                    if table_errors >= MAX_TABLE_ERRORS:
                        raise Blocked(
                            f"{table_errors} unexpected table failures"
                        ) from error
            done += 1
            logger.info(
                "Provider %s done: %d listed; decisions: %s; outcomes: %s; "
                "%d/%d requests spent",
                code,
                len(listed),
                ", ".join(f"{n} {why}" for why, n in sorted(decisions.items()))
                or "nothing to do",
                ", ".join(
                    f"{counts[k] - before[k]} {k}"
                    for k in ("harvested", "empty", "deferred", "failed")
                    if counts[k] - before[k]
                )
                or "no table work",
                pacer.sent,
                max_requests,
            )
            progress.update(processed=position, succeeded=counts["harvested"])
    except Blocked as error:
        interrupted = str(error)
        logger.error(
            "Run stopped early after %d of %d providers: %s; finished tables are "
            "kept and the rest waits for the next run",
            done,
            len(chosen),
            interrupted,
        )
        fail("run", "budget", error)
    progress.update(failed=counts["failed"])

    publish(tables, current, store, output)
    status = "completed_with_errors" if errors else "success"
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "interrupted": interrupted,
        "selection": sorted(wanted) if wanted else None,
        "counts": counts,
        "requests": requests,
        "deferred_tables": deferred,
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    if deferred:
        logger.info(
            "%d tables deferred beyond max_paging_requests=%d (about %d paging "
            "requests in total); they stay deferred until the limit is raised "
            "or the table changes",
            len(deferred),
            max_paging_requests,
            sum(d["paging_requests"] for d in deferred),
        )
    log = logger.info if status == "success" else logger.warning
    log(
        "EntryScape run finished: status=%s, %d requests, counts=%s, "
        "strategies=%s, %d errors, interrupted=%s",
        status,
        requests,
        counts,
        dict(strategies) or "none",
        len(errors),
        interrupted,
    )
    return result


def collect(data: Path, output: Path) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting EntryScape artifact %s (status %s, %d files)",
        result.get("run_id"),
        result.get("status"),
        len(result["manifest"]),
    )
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        logger.error("Published baseline changed since the run started; refusing")
        raise ValueError("Stale EntryScape artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        logger.error("Artifact checksums do not match its manifest; refusing")
        raise ValueError("EntryScape artifact checksum mismatch")
    for relative in result["manifest"]:
        if not _allowed_path(relative):
            raise ValueError(f"Unexpected EntryScape artifact path: {relative}")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
        logger.debug("Applied %s", relative)
    removed = 0
    for relative in harvest_manifest(data):
        if relative.startswith("source/") and relative not in result["manifest"]:
            (data / relative).unlink()
            removed += 1
    logger.info(
        "Collected %d files into %s, removed %d stale source files",
        len(result["manifest"]),
        data,
        removed,
    )
    return result

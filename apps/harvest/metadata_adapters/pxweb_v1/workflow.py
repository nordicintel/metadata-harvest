"""Refresh, reconciliation and provider workers; collectors are the only writers.

Workers store each table's source data (GET metadata, POST samples stripped of
observations, the listing row and variant locations' metadata) in a per-task source
store; collectors move it into ``data/pxweb_v1/source``. Documents are built later
by ``parse``. Checkpoints written before this layout carry no ``layout`` and are
refused when they still have pending work.
"""

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import unquote

from apps.harvest.artifacts import checkpoint_path, report_status
from apps.harvest.metadata_adapters.pxweb_v1.discovery import discover_provider
from apps.harvest.metadata_adapters.pxweb_v1.metadata import (
    get_pxweb_source,
    validate_raw,
)
from apps.harvest.source_store import Rejected, SourceStore, source_key
from apps.harvest.storage import (
    JsonlStore,
    filename,
    group_records,
    identity,
    location,
    now,
    read,
    timestamp,
    write,
)
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes

logger = logging.getLogger("pxweb_v1_logger")
_PROGRESS_EVERY = 25
LAYOUT = 2
"""Checkpoint layout: 2 = per-table source files; older checkpoints are refused."""
ADAPTER = "pxweb_v1"


def stored_key(record) -> str:
    """The source-store key of a listing row or task identity."""
    provider, code, language = identity(record)
    return source_key(provider, code, language)


def prepare(data, providers):
    current = checkpoint_path(data)
    state = read(current) if current.exists() else None
    logger.info(
        "Preparing pxweb_v1 run: %d configured providers, checkpoint %s",
        len(providers),
        "found" if state else "absent",
    )
    if state and any(task["status"] == "pending" for task in state["tasks"]):
        if state.get("layout") != LAYOUT:
            logger.error(
                "Checkpoint run %s predates the per-table source layout",
                state["run_id"],
            )
            raise ValueError(
                "Checkpoint predates per-dataset source layout; finish or discard "
                "the run before upgrading"
            )
        codes = sorted(
            {
                task["provider_code"]
                for task in state["tasks"]
                if task["status"] == "pending"
            }
        )
        if not set(codes).issubset(p["code"] for p in providers):
            logger.error(
                "Checkpoint run %s has pending work for providers no longer "
                "configured: %s",
                state["run_id"],
                sorted(set(codes) - {p["code"] for p in providers}),
            )
            raise ValueError(
                "Unfinished work contains a removed provider; reconcile its work list explicitly"
            )
        logger.info(
            "Resuming run %s from checkpoint: wave %s, %d pending tasks across "
            "providers %s, full=%s; skipping refresh and going straight to metadata",
            state["run_id"],
            state["wave"],
            sum(task["status"] == "pending" for task in state["tasks"]),
            codes,
            state.get("full", False),
        )
        return {
            "run_id": state["run_id"],
            "phase": "metadata",
            "full": state.get("full", False),
            "wave": state["wave"],
            "providers": codes,
        }
    run_id = now().replace("-", "").replace(":", "").replace("+0000", "Z")
    logger.info(
        "Starting new run %s: %s; refresh phase for providers %s",
        run_id,
        "no checkpoint to resume"
        if state is None
        else "checkpoint has no pending tasks (previous run finished)",
        [p["code"] for p in providers],
    )
    return {
        "run_id": run_id,
        "phase": "refresh",
        "wave": 0,
        "providers": [p["code"] for p in providers],
    }


async def refresh_provider(provider, run_id, output):
    progress = Progress("pxweb_v1", provider["code"])
    progress.update(phase="discovery", run_id=run_id, discovered=0)
    logger.info(
        "Refresh %s/%s: listing %d languages x %d databases from %s "
        "(languages=%s, databases=%s, rate_limit=%ss)",
        provider["code"],
        run_id,
        len(provider["languages"]),
        len(provider["database_ids"]),
        provider.get("base_web_url"),
        provider["languages"],
        provider["database_ids"],
        provider.get("rate_limit", 1),
    )
    result = {
        "provider_code": provider["code"],
        "run_id": run_id,
        "phase": "refresh",
        "started_at": now(),
        "scopes": [],
    }
    write(Path(output) / "result.json", result)
    try:
        async for scope in discover_provider(provider):
            result["scopes"].append(scope)
            logger.info(
                "Refresh %s scope %s/%s: %s, %d tables listed, %d observed ids, "
                "%d invalid ids, %d errors",
                provider["code"],
                scope["language"],
                scope.get("database_id", "listing"),
                "complete" if scope["complete"] else "INCOMPLETE",
                len(scope["records"]),
                len(scope["observed_ids"]),
                len(scope["invalid_ids"]),
                len(scope["errors"]),
            )
            progress.update(
                language=scope["language"],
                discovered=sum(len(s["records"]) for s in result["scopes"]),
                scopes_completed=len(result["scopes"]),
            )
            for error in scope["errors"]:
                logger.error(
                    "Refresh %s scope %s/%s %s error: %s",
                    provider["code"],
                    scope["language"],
                    scope.get("database_id", "listing"),
                    error.get("stage"),
                    error.get("message"),
                )
                progress.error(
                    "discovery",
                    scope.get("database_id", "listing"),
                    scope["language"],
                    str(error),
                )
            write(Path(output) / "result.json", result)
    finally:
        result["finished_at"] = now()
        write(Path(output) / "result.json", result)
    progress.update(
        phase="finished",
        result_status="completed_with_errors"
        if progress.state["error_count"]
        else "success",
    )
    logger.info(
        "Refresh %s finished: %d scopes (%d complete), %d tables listed, %d errors",
        provider["code"],
        len(result["scopes"]),
        sum(bool(s["complete"]) for s in result["scopes"]),
        sum(len(s["records"]) for s in result["scopes"]),
        progress.state["error_count"],
    )
    return result


def _scopes(providers, results, run_id):
    scopes, errors = {}, []
    received = {}
    for result in results:
        code = result["provider_code"]
        if (
            result.get("run_id") != run_id
            or result.get("phase") != "refresh"
            or code in received
        ):
            logger.error(
                "Rejecting refresh artifact for provider %s: run %r phase %r "
                "(expected run %s), duplicate=%s",
                code,
                result.get("run_id"),
                result.get("phase"),
                run_id,
                code in received,
            )
            raise ValueError("Duplicate or stale refresh artifact")
        received[code] = result
    expected_codes = {p["code"] for p in providers}
    if not set(received).issubset(expected_codes):
        logger.error(
            "Refresh artifacts for unconfigured providers %s; configured: %s",
            sorted(set(received) - expected_codes),
            sorted(expected_codes),
        )
        raise ValueError("Unexpected provider artifact")
    for provider in providers:
        code = provider["code"]
        rows = received.get(code, {}).get("scopes", [])
        indexed = {(row["language"], row["database_id"]): row for row in rows}
        if len(indexed) != len(rows):
            logger.error(
                "Provider %s delivered %d scopes but only %d distinct "
                "language/database pairs",
                code,
                len(rows),
                len(indexed),
            )
            raise ValueError("Duplicate refresh scope")
        for language in provider["languages"]:
            for database in provider["database_ids"]:
                database = unquote(database)
                scope = indexed.get(
                    (language, database),
                    {
                        "language": language,
                        "database_id": database,
                        "complete": False,
                        "records": [],
                        "observed_ids": [],
                        "invalid_ids": [],
                        "errors": [
                            {
                                "stage": "job",
                                "message": "Provider job did not deliver this scope",
                            }
                        ],
                    },
                )
                if (language, database) not in indexed:
                    logger.warning(
                        "Provider %s delivered no scope for %s/%s; treating it as "
                        "incomplete so prior metadata for it is protected",
                        code,
                        language,
                        database,
                    )
                scopes[(code, language, database)] = scope
                errors.extend(
                    {
                        "provider_code": code,
                        "language": language,
                        "database_id": database,
                        **error,
                    }
                    for error in scope["errors"]
                )
    return scopes, errors


def collect_refresh(data, providers, results, run_id, *, full=False):
    data = Path(data)
    logger.info(
        "Collecting refresh for run %s: %s run, %d provider artifacts for %d "
        "configured providers",
        run_id,
        "FULL (every listed table is re-fetched)"
        if full
        else "incremental (unchanged tables are reused)",
        len(results),
        len(providers),
    )
    last_report = data / "reports/harvest.json"
    previous_failures = {}
    if last_report.exists():
        for error in read(last_report).get("errors", []):
            try:
                previous_failures[filename(error)] = error
            except (KeyError, ValueError, TypeError):
                continue  # Legacy reports may lack a complete dataset identity.
    if previous_failures:
        logger.info(
            "%d tables failed in the previous run and will be retried even if "
            "unchanged",
            len(previous_failures),
        )
    scopes, errors = _scopes(providers, results, run_id)
    incomplete = [key for key, scope in scopes.items() if not scope["complete"]]
    logger.info(
        "Refresh scopes: %d total, %d complete, %d incomplete, %d errors",
        len(scopes),
        len(scopes) - len(incomplete),
        len(incomplete),
        len(errors),
    )
    for key in incomplete:
        logger.warning(
            "Scope %s/%s/%s is incomplete: its tables are protected from "
            "removal and from being replaced by archive entries",
            *key,
        )
    fresh = []
    for (code, _, _), scope in scopes.items():
        fresh.extend(
            {**row, "provider_code": code, "dataset_code": row["id"]}
            for row in scope["records"]
        )
    grouped = {identity(row): row for row in group_records(fresh)}
    listings = JsonlStore(data / "table-records")
    store = SourceStore(data / "source", ADAPTER)
    previous = {identity(row): row for row in listings.records.values()}
    previous_names = set(listings.records)
    known = {}
    for key in store.entries():
        saved = store.get(key)["responses"]["table"]
        name = filename(saved)
        if stored_key(saved) != key:
            raise ValueError(f"Stored source identity mismatch: {key}")
        known[name] = key
        if name not in previous_names:
            previous.setdefault(identity(saved), saved)
    logger.info(
        "Candidate set: %d freshly listed rows -> %d distinct tables; %d "
        "previously known tables (%d listings, %d stored source entries)",
        len(fresh),
        len(grouped),
        len(previous),
        len(previous_names),
        len(known),
    )
    tasks, verified = [], []
    counts = dict(reused=0, removed=0, deferred=0)
    reasons = {}

    def protected(key, place):
        scope = scopes.get((key[0], key[2], place["database_id"]))
        return scope is None or not scope["complete"] or key[1] in scope["invalid_ids"]

    for key in sorted(set(previous) | set(grouped)):
        old, new = previous.get(key), grouped.get(key)
        name = filename(new or old)
        if new is None:
            places = old.get("paths", [old])
            absent = all(
                not protected(key, place)
                and key[1]
                not in scopes[(key[0], key[2], place["database_id"])]["observed_ids"]
                for place in places
            )
            if absent:
                logger.debug(
                    "Removing %s: no longer listed in any complete scope", name
                )
                if name in known:
                    store.remove(known.pop(name))
                    counts["removed"] += 1
                listings.records.pop(name, None)
            else:
                logger.debug(
                    "Keeping %s though not listed: a scope was incomplete, "
                    "invalid or still observes the id",
                    name,
                )
                counts["deferred"] += 1
            continue
        if old:
            # Keep locations from failed scopes, but don't claim them as fresh.
            retained = [
                place for place in old.get("paths", [old]) if protected(key, place)
            ]
            incoming = {location(place) for place in new["paths"]}
            for place in retained:
                if location(place) not in incoming:
                    new["paths"].append(place)
            new["paths"].sort(key=location)
            # A failed canonical scope cannot replace newer saved metadata with an archive.
            if protected(key, old) and location(old) not in incoming:
                counts["deferred"] += 1
                logger.debug(
                    "%s: canonical location is in a failed scope and was not "
                    "relisted; %s",
                    name,
                    "keeping saved record (listing is not newer)"
                    if timestamp(new["published"]) <= timestamp(old["published"])
                    else "accepting the newer listing from another location",
                )
                if timestamp(new["published"]) <= timestamp(old["published"]):
                    new = {**old, "paths": new["paths"]}
                    listings.records[name] = new
                    continue
        listings.records[name] = new
        verified.append(
            {
                "provider_code": key[0],
                "dataset_code": key[1],
                "language": key[2],
                "updated": new["published"],
            }
        )
        reason = "no stored source"
        if name in known:
            try:
                existing = store.get(known[name])
                validate_raw(existing, new)
                if (
                    not full
                    and name not in previous_failures
                    and existing["updated"] == new["published"]
                ):
                    counts["reused"] += 1
                    logger.debug(
                        "%s: reuse stored source, published %s unchanged",
                        name,
                        new["published"],
                    )
                    continue
                reason = (
                    "full run"
                    if full
                    else "previous failure"
                    if name in previous_failures
                    else "published date changed"
                )
            except (ValueError, KeyError, TypeError) as error:
                reason = "stored source invalid"
                logger.warning(
                    "%s: stored source unusable (%s); re-fetching", name, error
                )
                errors.append(
                    {
                        "provider_code": key[0],
                        "dataset_code": key[1],
                        "language": key[2],
                        "stage": "saved_metadata",
                        "message": str(error),
                    }
                )
        reasons[reason] = reasons.get(reason, 0) + 1
        logger.debug("%s: queued for fetch (%s)", name, reason)
        tasks.append(
            {
                "filename": name,
                "provider_code": key[0],
                "status": "pending",
                "kind": "updated" if name in known else "created",
            }
        )
    started_at = min(
        (result["started_at"] for result in results if result.get("started_at")),
        default=now(),
    )
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": now(),
        "records": verified,
        "successful_records": len(verified),
        "error_count": len(errors),
        "errors": errors,
        "scopes": [
            {
                "provider_code": key[0],
                "language": key[1],
                "database_id": key[2],
                "complete": value["complete"],
                "record_count": len(value["records"]),
            }
            for key, value in sorted(scopes.items())
        ],
    }
    logger.info(
        "Selection: %d verified, %d reused unchanged, %d queued for fetch, "
        "%d removed, %d deferred (protected by incomplete scopes)",
        len(verified),
        counts["reused"],
        len(tasks),
        counts["removed"],
        counts["deferred"],
    )
    if reasons:
        logger.info(
            "Queued for fetch by reason: %s",
            ", ".join(f"{reason}={n}" for reason, n in sorted(reasons.items())),
        )
    logger.info(
        "Queued tasks per provider: %s",
        {
            code: sum(t["provider_code"] == code for t in tasks)
            for code in sorted({t["provider_code"] for t in tasks})
        },
    )
    refresh_name = "refresh.json"
    listings.save()
    store.save()
    write(data / "reports" / refresh_name, report)
    state = {
        "run_id": run_id,
        "layout": LAYOUT,
        "stage": "metadata",
        "full": full,
        "deferred_failures": [
            error
            for name, error in previous_failures.items()
            if name in listings.records
            and name not in {task["filename"] for task in tasks}
        ],
        "wave": 1,
        "started_at": started_at,
        "refresh_report": refresh_name,
        "counts": counts,
        "refresh_errors": errors,
        "tasks": tasks,
        "no_progress_waves": 0,
    }
    write(data / "work/current.json", state)
    logger.info(
        "Refresh collected for run %s: %d tasks pending, %d deferred failures "
        "carried over; wrote work/current.json at wave 1",
        run_id,
        len(tasks),
        len(state["deferred_failures"]),
    )
    return state


async def build_provider(
    data, provider, run_id, wave, output, *, budget_seconds=19_200
):
    data, output = Path(data), Path(output)
    state = read(data / "work/current.json")
    if (state["run_id"], state["wave"]) != (run_id, wave):
        raise ValueError("Stale metadata work list")
    progress = Progress("pxweb_v1", provider["code"])
    progress.update(
        phase="metadata",
        run_id=run_id,
        wave=wave,
        full=state.get("full", False),
        total=sum(
            task["status"] == "pending" and task["provider_code"] == provider["code"]
            for task in state["tasks"]
        ),
    )
    result = {
        "provider_code": provider["code"],
        "run_id": run_id,
        "wave": wave,
        "phase": "metadata",
        "started_at": now(),
        "outcomes": [],
    }
    write(output / "result.json", result)
    listings = JsonlStore(data / "table-records")
    completed = SourceStore(output / "source", ADAPTER)
    deadline = time.monotonic() + budget_seconds
    mine = [
        task
        for task in state["tasks"]
        if task["status"] == "pending" and task["provider_code"] == provider["code"]
    ]
    logger.info(
        "Build %s run %s wave %d: %d of %d tasks in the work list are pending "
        "for this provider (the rest belong to other providers or are done); "
        "full=%s, budget=%ss, rate_limit=%ss",
        provider["code"],
        run_id,
        wave,
        len(mine),
        len(state["tasks"]),
        state.get("full", False),
        budget_seconds,
        provider.get("rate_limit", 1),
    )
    for task in state["tasks"]:
        if task["status"] != "pending" or task["provider_code"] != provider["code"]:
            continue
        if time.monotonic() + 900 > deadline:
            logger.info(
                "Build %s: time budget exhausted (less than 900s left of %ss); "
                "stopping with %d of %d tasks attempted, the rest carry to the "
                "next wave",
                provider["code"],
                budget_seconds,
                len(result["outcomes"]),
                len(mine),
            )
            break
        outcome = {"filename": task["filename"], "started_at": now()}
        logger.debug(
            "Build %s: fetching %s (%s)",
            provider["code"],
            task["filename"],
            task.get("kind"),
        )
        progress.update(current=task["filename"], operation="metadata")
        try:
            record = listings.records[task["filename"]]
            if filename(record) != task["filename"]:
                raise ValueError("Table-record filename does not match its identity")
            outcome.update(
                dataset_code=record["dataset_code"],
                language=record["language"],
                api_url=record["api_url"],
            )
            async with asyncio.timeout(900):
                raw = await get_pxweb_source(record)
            validate_raw(raw, record)
            try:
                completed.put(
                    stored_key(record),
                    responses=raw["responses"],
                    derived=raw["derived"],
                    updated=record["published"],
                    retrieved_at=now(),
                )
                completed.save()
            except Exception:
                completed = SourceStore(output / "source", ADAPTER)  # drop pending
                raise
            outcome["status"] = "success"
        except (Rejected, Exception) as error:
            outcome.update(status="failed", error=f"{type(error).__name__}: {error}")
            logger.error(
                "Build %s: %s failed, recorded and continuing: %s",
                provider["code"],
                task["filename"],
                outcome["error"],
            )
            progress.error(
                "metadata",
                outcome.get("dataset_code", task["filename"]),
                outcome.get("language", "unknown"),
                outcome["error"],
                url=outcome.get("api_url"),
            )
        outcome["finished_at"] = now()
        result["outcomes"].append(outcome)
        progress.update(
            processed=len(result["outcomes"]),
            succeeded=sum(o["status"] == "success" for o in result["outcomes"]),
            failed=sum(o["status"] == "failed" for o in result["outcomes"]),
        )
        write(output / "result.json", result)
        logger.debug(
            "Build %s: %s -> %s", provider["code"], task["filename"], outcome["status"]
        )
        if len(result["outcomes"]) % _PROGRESS_EVERY == 0:
            logger.info(
                "Build %s progress: %d of %d tasks done, %d succeeded, %d failed",
                provider["code"],
                len(result["outcomes"]),
                len(mine),
                sum(o["status"] == "success" for o in result["outcomes"]),
                sum(o["status"] == "failed" for o in result["outcomes"]),
            )
        await asyncio.sleep(provider.get("rate_limit", 1))
    result["finished_at"] = now()
    write(output / "result.json", result)
    logger.info(
        "Build %s finished: %d attempted, %d succeeded, %d failed, %d left for "
        "later waves",
        provider["code"],
        len(result["outcomes"]),
        sum(o["status"] == "success" for o in result["outcomes"]),
        sum(o["status"] == "failed" for o in result["outcomes"]),
        len(mine) - len(result["outcomes"]),
    )
    progress.update(
        phase="finished",
        result_status="running"
        if progress.state["remaining"]
        else "completed_with_errors"
        if progress.state["failed"]
        else "success",
        current=None,
    )
    return result


def collect_metadata(data, result_paths, run_id, wave):
    data = Path(data)
    state = read(data / "work/current.json")
    if (state["run_id"], state["wave"]) != (run_id, wave):
        raise ValueError("Stale metadata collector")
    listings = JsonlStore(data / "table-records")
    active = SourceStore(data / "source", ADAPTER)
    tasks = {task["filename"]: task for task in state["tasks"]}
    seen, providers = set(), set()
    progress = 0
    logger.info(
        "Collecting metadata for run %s wave %d: %d result artifacts, %d tasks "
        "in the work list (%d still pending)",
        run_id,
        wave,
        len(result_paths),
        len(tasks),
        sum(task["status"] == "pending" for task in tasks.values()),
    )
    for path in result_paths:
        path = Path(path)
        result = read(path)
        if (result.get("run_id"), result.get("wave"), result.get("phase")) != (
            run_id,
            wave,
            "metadata",
        ) or result["provider_code"] in providers:
            raise ValueError("Duplicate or stale metadata artifact")
        providers.add(result["provider_code"])
        completed = SourceStore(path.parent / "source", ADAPTER)
        for outcome in result["outcomes"]:
            name = outcome["filename"]
            task = tasks.get(name)
            if (
                task is None
                or task["provider_code"] != result["provider_code"]
                or name in seen
                or task["status"] != "pending"
            ):
                raise ValueError("Unknown, duplicate or completed task outcome")
            seen.add(name)
            if outcome["status"] not in {"success", "failed"}:
                raise ValueError("Invalid task outcome status")
            if outcome["status"] == "success":
                record = listings.records[name]
                if filename(record) != name:
                    raise ValueError("Unsafe artifact filename")
                envelope = completed.get(stored_key(record))
                if envelope is None:
                    raise ValueError("Artifact lacks the stored source entry")
                validate_raw(envelope, record)
                active.put(
                    stored_key(record),
                    responses=envelope["responses"],
                    derived=envelope["derived"],
                    updated=record["published"],
                    retrieved_at=envelope["retrieved_at"],
                )
            task.update(outcome)
            progress += 1
        logger.info(
            "Accepted %d outcomes from provider %s (%d succeeded)",
            len(result["outcomes"]),
            result["provider_code"],
            sum(o["status"] == "success" for o in result["outcomes"]),
        )
    pending = [task for task in tasks.values() if task["status"] == "pending"]
    state["no_progress_waves"] = 0 if progress else state["no_progress_waves"] + 1
    state["wave"] += 1
    state["stage"] = "metadata" if pending else "complete"
    if pending and state["no_progress_waves"] >= 3:
        state["stage"] = "interrupted"
        logger.error(
            "Run %s interrupted: %d consecutive waves made no progress with %d "
            "tasks still pending",
            run_id,
            state["no_progress_waves"],
            len(pending),
        )
    elif pending and not progress:
        logger.warning(
            "Wave %d made no progress (%d consecutive); %d tasks pending",
            wave,
            state["no_progress_waves"],
            len(pending),
        )
    counts = {
        **state["counts"],
        "created": 0,
        "updated": 0,
        "failed": len(state.get("deferred_failures", [])),
        "unfinished": len(pending),
    }
    for task in tasks.values():
        if task["status"] == "success":
            counts[task["kind"]] += 1
        elif task["status"] == "failed":
            counts["failed"] += 1
    status = report_status(
        pending=len(pending),
        errors=[True] if counts["failed"] or state["refresh_errors"] else [],
        interrupted=state["stage"] == "interrupted",
    )
    report = {
        "run_id": run_id,
        "status": status,
        "full": state.get("full", False),
        "started_at": state["started_at"],
        "wave": wave,
        "updated_at": now(),
        "counts": counts,
        "refresh_report": state["refresh_report"],
        "refresh_error_count": len(state["refresh_errors"]),
        "errors": [
            {
                "provider_code": task["provider_code"],
                **{
                    key: task.get(key)
                    for key in ("dataset_code", "language", "api_url", "error")
                },
            }
            for task in tasks.values()
            if task["status"] == "failed"
        ]
        + state.get("deferred_failures", []),
    }
    if not pending:
        report["finished_at"] = report["updated_at"]
    active.save()
    check_collection_sizes(data)
    write(data / "reports/harvest.json", report)
    if pending:
        write(data / "work/current.json", state)
    else:
        (data / "work/current.json").unlink(missing_ok=True)
    proceed = bool(pending) and state["stage"] != "interrupted"
    logger.info(
        "Wave %d collected for run %s: status=%s, %d outcomes accepted, %d "
        "pending; %s; counts=%s",
        wave,
        run_id,
        status,
        progress,
        len(pending),
        "another wave will continue the remaining work"
        if proceed
        else "run complete (checkpoint removed)"
        if not pending
        else "run stops here (checkpoint kept for a later resume)",
        counts,
    )
    return {
        "continue": proceed,
        "status": status,
        "counts": counts,
    }

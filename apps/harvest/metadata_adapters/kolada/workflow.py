"""Incremental orchestration with independent KPI/scope failures."""

import logging
import shutil
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from apps.harvest.metadata_adapters.kolada.client import (
    BudgetExpired,
    KoladaClient,
    chunks,
)
from apps.harvest.metadata_adapters.kolada.metadata import (
    build_dataset,
    context_digest,
    context_for,
    reference_rows,
)
from apps.harvest.metadata_adapters.kolada.state import (
    COLLECTION,
    STATE,
    load_documents,
    load_state,
    save,
)
from apps.harvest.storage import read, write
from apps.harvest.validation import validate_metadata
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest

logger = logging.getLogger("kolada_logger")


async def discover_provider(provider: dict, *, client=None) -> dict:
    """Return fully paginated native listings using an injectable connection."""
    connection = client or KoladaClient(provider)
    logger.info("Discovering Kolada native listings (full pagination)")
    try:
        listings = {}
        for name in (
            "kpi",
            "municipality",
            "ou",
            "municipality_groups",
            "kpi_groups",
        ):
            listings[name] = await connection.listing(name)
            logger.info("Discovered %d %s rows", len(listings[name]), name)
        return listings
    finally:
        if client is None:
            await connection.close()


async def run(
    provider: dict,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    kpis=None,
    scope=None,
    resume=False,
    budget_seconds=19200,
) -> dict:
    """Refresh signals first, then publish only validated changed replacements.

    The optional KPI/scope restriction applies to all per-KPI API work. Listings
    are small native metadata catalogues, never an observation-wide rescrape.
    Explicit continuations reuse completed checks; ordinary runs check fresh
    signals while retaining failed replacements in the pending queue.
    """
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    baseline = manifest(data)
    state, documents = load_state(data), load_documents(data)
    if not (data / STATE).exists():
        raise ValueError("Kolada current state is missing from the checkout")
    logger.info(
        "Kolada refresh start: kpis=%s scope=%s resume=%s budget=%ss; "
        "checkout holds %d KPIs, %d scopes, %d pending, %d accepted documents",
        sorted(kpis) if kpis else None,
        scope,
        resume,
        budget_seconds,
        len(state["kpis"]),
        len(state["scopes"]),
        len(state["pending"]),
        len(documents),
    )
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, dest)
    current = data / "work/current.json"
    previous = read(current) if resume and current.exists() else {}
    if resume and not previous:
        logger.info("Resume requested but no checkpoint exists; starting a new run")
    if previous.get("selection") and kpis is None and scope is None:
        kpis = previous["selection"]["kpis"]
        scope = previous["selection"]["scope"]
        logger.info(
            "Resuming checkpoint run %s: inherited selection kpis=%s scope=%s",
            previous.get("run_id"),
            kpis,
            scope,
        )
    elif previous:
        logger.info(
            "Resuming checkpoint run %s with explicit selection kpis=%s scope=%s "
            "(checkpoint selection not inherited)",
            previous.get("run_id"),
            kpis,
            scope,
        )
    run_id = previous.get("run_id", uuid4().hex)
    wave = previous.get("wave", 0) + 1
    logger.info(
        "Run %s wave %d (%s); effective selection kpis=%s scope=%s",
        run_id,
        wave,
        "continuation of checkpointed run" if previous else "fresh run",
        sorted(kpis) if kpis else "all",
        scope or "all",
    )
    started = clock or datetime.now(UTC)
    stamp = started.isoformat()
    deadline = monotonic() + budget_seconds
    connection = client or KoladaClient(provider)
    if isinstance(connection, KoladaClient):
        connection.deadline = deadline
    progress = Progress("kolada", "kolada")
    progress.update(phase="discovery", run_id=run_id, wave=wave)
    counts = {"updated": 0, "removed": 0, "failed": 0, "reused": 0, "checked": 0}
    errors = []
    incomplete = False
    selected = set(kpis) if kpis else None
    saved_changes = 0

    def chosen(code):
        kind, identifier = code.split("/", 1)
        return (selected is None or identifier in selected) and (
            scope is None or kind == scope
        )

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        logger.error("Recorded %s failure for %s: %s", stage, resource, entry["error"])
        progress.error(stage, resource, "sv", entry["error"])

    def persist():
        nonlocal saved_changes
        changes = counts["updated"] + counts["removed"]
        logger.debug(
            "Persisting state (metadata rewritten: %s, %d pending)",
            changes != saved_changes,
            len(state["pending"]),
        )
        save(output, state, documents, write_metadata=changes != saved_changes)
        saved_changes = changes
        checkpoint = output / "work/current.json"
        if state["pending"] or incomplete:
            logger.debug(
                "Writing checkpoint: %d pending, incomplete=%s",
                len(state["pending"]),
                incomplete,
            )
            write(
                checkpoint,
                {
                    "run_id": run_id,
                    "wave": wave,
                    "selection": {
                        "kpis": sorted(selected) if selected else None,
                        "scope": scope,
                    },
                },
            )
        else:
            logger.debug("No pending work; removing checkpoint")
            checkpoint.unlink(missing_ok=True)

    try:
        complete = True
        try:
            progress.update(current="kpi", operation="catalogue")
            catalogue = await connection.listing("kpi")
            logger.info("Fetched KPI catalogue: %d rows", len(catalogue))
            native = {}
            for row in catalogue:
                identifier = row.get("id")
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or not isinstance(row.get("title"), str)
                    or not row["title"]
                    or identifier in native
                ):
                    fail(
                        "discovery",
                        str(identifier),
                        ValueError("Malformed or duplicate KPI"),
                    )
                    complete = False
                    continue
                native[identifier] = row
            logger.info(
                "KPI catalogue validated: %d usable KPIs, complete=%s",
                len(native),
                complete,
            )
        except Exception as error:
            if isinstance(error, BudgetExpired):
                raise
            fail("discovery", "kpi", error)
            native, complete = {}, False
        if not complete:
            logger.warning(
                "KPI catalogue incomplete; skipping removal detection and keeping "
                "the %d KPIs already in state",
                len(state["kpis"]),
            )
        if complete:
            missing = sorted(set(state["kpis"]) - set(native))
            logger.info(
                "%d known KPIs are absent from the catalogue; confirming removal "
                "individually (only those inside the selection)",
                len(missing),
            )
            for identifier in missing:
                if selected is not None and identifier not in selected:
                    logger.debug(
                        "Skipping removal check for %s: outside selected KPIs",
                        identifier,
                    )
                    continue
                try:
                    progress.update(current=identifier, operation="confirm removal")
                    row = await connection.kpi(identifier)
                    if row is None:
                        logger.info(
                            "KPI %s confirmed removed at source; dropping its "
                            "selected scopes",
                            identifier,
                        )
                        for code in list(state["scopes"]):
                            if code.split("/", 1)[1] == identifier and chosen(code):
                                state["scopes"].pop(code)
                                state["pending"].pop(code, None)
                                if documents.pop(code, None) is not None:
                                    counts["removed"] += 1
                        if not any(
                            c.endswith("/" + identifier) for c in state["scopes"]
                        ):
                            state["kpis"].pop(identifier, None)
                    else:
                        logger.info(
                            "KPI %s missing from catalogue but still served "
                            "individually; keeping it",
                            identifier,
                        )
                        native[identifier] = row
                except Exception as error:
                    if isinstance(error, BudgetExpired):
                        raise
                    fail("discovery", identifier, error)
        state["kpis"].update(native)
        logger.info("State now tracks %d KPIs", len(state["kpis"]))
        for name in ("municipality", "ou", "municipality_groups", "kpi_groups"):
            try:
                progress.update(current=name, operation="catalogue")
                rows = await connection.listing(name)
                if any(
                    not isinstance(row.get("id"), str) or not row["id"] for row in rows
                ) or len({r["id"] for r in rows}) != len(rows):
                    raise ValueError(f"Malformed/duplicate {name} catalogue")
                state["references"][name] = reference_rows(rows)
                logger.info("Reference catalogue %s: %d rows", name, len(rows))
            except Exception as error:
                if isinstance(error, BudgetExpired):
                    raise
                fail("discovery", name, error)
                logger.warning(
                    "Reference catalogue %s not refreshed; keeping %d saved rows",
                    name,
                    len(state["references"].get(name, {})),
                )

        batches = defaultdict(list)
        skipped = defaultdict(int)
        queued = {"new scope": 0, "missing document": 0, "stale metadata": 0}
        for identifier, kpi in sorted(state["kpis"].items()):
            for kind in ("municipality", "ou"):
                code = f"{kind}/{identifier}"
                if not chosen(code):
                    skipped["outside kpi/scope selection"] += 1
                    continue
                if (
                    kind == "ou"
                    and not kpi.get("has_ou_data")
                    and code not in state["scopes"]
                ):
                    skipped["ou scope without ou data"] += 1
                    continue
                years = list(range(1994 if kind == "ou" else 1970, started.year + 6))
                entry = state["scopes"].get(code)
                if entry is None:
                    entry = {"coverage": {}, "periods": years}
                    state["scopes"][code] = entry
                    state["pending"][code] = "data"
                    queued["new scope"] += 1
                    logger.debug("%s: new scope, queued for data collection", code)
                document = documents.get(code)
                if document is None and not entry.get("empty"):
                    if code not in state["pending"]:
                        queued["missing document"] += 1
                        logger.debug(
                            "%s: no accepted document and scope not empty; queued",
                            code,
                        )
                    state["pending"].setdefault(
                        code, "metadata" if entry["coverage"].get("years") else "data"
                    )
                if document:
                    saved = document["extension"]["kolada"]
                    context = context_for(
                        kpi, kind, entry["coverage"], state["references"]
                    )
                    if saved["source_record"] != kpi or saved.get(
                        "references_digest"
                    ) != context_digest(context):
                        if code not in state["pending"]:
                            queued["stale metadata"] += 1
                            logger.debug(
                                "%s: KPI record or reference digest changed; "
                                "queued for metadata rebuild",
                                code,
                            )
                        state["pending"].setdefault(code, "metadata")
                checkpoint = (
                    entry.get("data_checked_at")
                    or entry.get("coverage_at")
                    or entry.get("checked_at")
                )
                cutoff = (
                    (
                        datetime.fromisoformat(checkpoint).date() - timedelta(days=1)
                    ).isoformat()
                    if checkpoint
                    else "1970-01-01"
                )
                if entry.get("checked_run") == run_id:
                    skipped["already change-checked in this run"] += 1
                    logger.debug("%s: change check already done in run", code)
                    continue
                logger.debug(
                    "%s: change check cutoff %s (checkpoint %s)",
                    code,
                    cutoff,
                    checkpoint or "none, full history",
                )
                batches[(kind, cutoff, tuple(years))].append(identifier)
        total_checks = sum(map(len, batches.values()))
        logger.info(
            "Change-check candidates: %d scopes in %d cutoff groups; skipped %s",
            total_checks,
            len(batches),
            dict(skipped) or "none",
        )
        logger.info(
            "Pending work queued before probing: %s; %d pending in total",
            queued,
            len(state["pending"]),
        )
        progress.update(phase="changes", total=total_checks, processed=0)
        pending_before_checks = len(state["pending"])
        for (kind, cutoff, periods), identifiers in batches.items():
            logger.info(
                "Checking %d %s KPIs for changes since %s",
                len(identifiers),
                kind,
                cutoff,
            )
            for batch in chunks(identifiers, 25):
                if monotonic() >= deadline:
                    if not incomplete:
                        logger.warning(
                            "Time budget exhausted during change checks; "
                            "remaining batches deferred to the next wave"
                        )
                    incomplete = True
                    break

                async def check(ids, kind=kind, periods=periods, cutoff=cutoff):
                    try:
                        progress.update(
                            current=f"{kind}/{','.join(ids)}", operation="change check"
                        )
                        hits = await connection.changed(
                            kind, ids, list(periods), cutoff
                        )
                    except Exception as error:
                        if isinstance(error, BudgetExpired):
                            raise
                        if len(ids) > 1:
                            logger.warning(
                                "Change check for %d %s KPIs failed (%s: %s); "
                                "retrying individually",
                                len(ids),
                                kind,
                                type(error).__name__,
                                error,
                            )
                            for identifier in ids:
                                await check([identifier])
                        else:
                            fail("changes", f"{kind}/{ids[0]}", error)
                        return
                    logger.debug(
                        "Change check %s/%s since %s: %d of %d changed",
                        kind,
                        ",".join(ids),
                        cutoff,
                        len(hits),
                        len(ids),
                    )
                    for identifier in ids:
                        code = f"{kind}/{identifier}"
                        if identifier in hits:
                            state["pending"][code] = "data"
                        entry = state["scopes"][code]
                        # Detected work and this marker are saved together below.
                        entry["data_checked_at"] = stamp
                        entry["checked_at"] = stamp
                        entry["checked_run"] = run_id
                        entry["periods"] = list(periods)
                        counts["checked"] += 1
                    progress.update(
                        processed=counts["checked"], current=f"{kind}/{ids[-1]}"
                    )

                await check(batch)
        logger.info(
            "Change checks done: %d scopes checked, %d newly flagged for data "
            "refresh (pending %d -> %d)",
            counts["checked"],
            len(state["pending"]) - pending_before_checks,
            pending_before_checks,
            len(state["pending"]),
        )
        persist()
        tasks = [code for code in sorted(state["pending"]) if chosen(code)]
        counts["reused"] = sum(chosen(c) and c not in tasks for c in documents)
        by_reason = defaultdict(int)
        for code in tasks:
            by_reason[state["pending"][code]] += 1
        logger.info(
            "Selected %d of %d pending scopes for rebuild (%s); %d accepted "
            "documents reused unchanged; %d pending outside selection",
            len(tasks),
            len(state["pending"]),
            dict(by_reason) or "none",
            counts["reused"],
            len(state["pending"]) - len(tasks),
        )
        progress.update(
            phase="metadata", total=len(tasks), processed=0, reused=counts["reused"]
        )
        for position, code in enumerate(tasks):
            if monotonic() >= deadline:
                incomplete = True
                logger.warning(
                    "Time budget exhausted after %d of %d scopes; %d deferred to "
                    "the next wave",
                    position,
                    len(tasks),
                    len(tasks) - position,
                )
                break
            kind, identifier = code.split("/", 1)
            logger.debug("%s: rebuilding (%s)", code, state["pending"][code])
            progress.update(current=code, operation=state["pending"][code])
            entry = state["scopes"][code]
            try:
                coverage = entry["coverage"]
                modified = entry.get("modified")
                if state["pending"][code] == "data":
                    logger.debug(
                        "%s: probing modification date, then collecting coverage",
                        code,
                    )
                    # Probe first; collecting afterwards keeps the accepted coverage at least this fresh.
                    modified = await connection.modified(
                        kind,
                        identifier,
                        entry["periods"],
                        earliest=date.fromisoformat(entry["modified"])
                        if entry.get("modified")
                        else date(1970, 1, 1),
                        today=started.date(),
                    )
                    coverage = await connection.coverage(
                        kind, identifier, entry["periods"], state["references"]
                    )
                if coverage.get("years"):
                    replacement = build_dataset(
                        state["kpis"][identifier],
                        kind,
                        coverage,
                        state["references"],
                        modified=modified,
                    )
                    validate_metadata(replacement, adapter="kolada")
                    documents[code] = replacement
                    counts["updated"] += 1
                    logger.debug(
                        "%s: validated and accepted (%d years, modified %s)",
                        code,
                        len(coverage["years"]),
                        modified,
                    )
                else:
                    if documents.pop(code, None) is not None:
                        counts["removed"] += 1
                        logger.info("%s: no data left; removed accepted document", code)
                    else:
                        logger.debug("%s: no data and no document; marked empty", code)
                entry.update(
                    coverage=coverage,
                    modified=modified,
                    empty=not coverage.get("years"),
                )
                if state["pending"][code] == "data":
                    entry["coverage_at"] = stamp
                state["pending"].pop(code)
            except Exception as error:
                if isinstance(error, BudgetExpired):
                    raise
                fail("metadata", code, error)
                counts["failed"] += 1
                logger.warning(
                    "%s: rebuild failed; keeping the previous document and "
                    "leaving it pending",
                    code,
                )
            progress.update(
                processed=position + 1,
                succeeded=counts["updated"],
                failed=counts["failed"],
            )
            if (position + 1) % 25 == 0:
                logger.info(
                    "Rebuilt %d of %d scopes (%d updated, %d removed, %d failed)",
                    position + 1,
                    len(tasks),
                    counts["updated"],
                    counts["removed"],
                    counts["failed"],
                )
                persist()
    except BudgetExpired:
        incomplete = True
        logger.warning(
            "Request budget expired mid-request; ending this wave with partial progress"
        )
    finally:
        if client is None:
            await connection.close()
    persist()
    status = (
        "completed_with_errors" if errors else "running" if incomplete else "success"
    )
    logger.info(
        "Run %s wave %d finished: status=%s continue=%s counts=%s errors=%d "
        "pending=%d requests=%s",
        run_id,
        wave,
        status,
        incomplete and not errors,
        counts,
        len(errors),
        len(state["pending"]),
        getattr(connection, "requests", "n/a"),
    )
    result = {
        "run_id": run_id,
        "wave": wave,
        "status": status,
        "continue": incomplete and not errors,
        "counts": counts,
        "errors": errors,
        "pending": len(state["pending"]),
        "selection": {"kpis": sorted(selected) if selected else None, "scope": scope},
    }
    write(output / "reports/latest.json", result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    return result


def collect(data: Path, output: Path) -> dict:
    """Verify current artifact bytes and baseline, never revalidate trusted datasets."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting Kolada worker artifact: status=%s, %d files",
        result.get("status"),
        len(result["manifest"]),
    )
    if manifest(data) != result["base_manifest"]:
        raise ValueError("Stale Kolada artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Kolada artifact checksum mismatch")
    allowed = {
        COLLECTION,
        STATE,
        "reports/latest.json",
        "work/current.json",
    }
    if set(result["manifest"]) - allowed:
        raise ValueError("Unexpected Kolada artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        logger.debug("Collecting %s", relative)
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    if "work/current.json" not in result["manifest"]:
        logger.info("Run complete; clearing checkpoint work/current.json")
        (data / "work/current.json").unlink(missing_ok=True)
    return result

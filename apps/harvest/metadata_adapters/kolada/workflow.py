"""Incremental orchestration: one change scan, then per-KPI rebuilds or rescans.

Every run lists the KPI catalogue and reference tables, scans all cells added or
deleted since the last completed refresh, folds additions into saved coverage and
rescans KPIs with deletions from scratch. Only affected KPI scopes are rebuilt and
validated; failures stay pending for the next run and never erase accepted data.
"""

import logging
import shutil
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from apps.harvest.metadata_adapters.kolada import coverage as cov
from apps.harvest.metadata_adapters.kolada.client import BudgetExpired, KoladaClient
from apps.harvest.metadata_adapters.kolada.metadata import (
    build_dataset,
    context_digest,
    context_for,
    load_municipalities,
    reference_rows,
    updated_for,
    years_for,
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

SCOPES = ("municipality", "ou")
LISTINGS = ("ou", "municipality_groups", "kpi_groups")


async def discover_provider(provider: dict, *, client=None) -> dict:
    """Return fully paginated native listings using an injectable connection."""
    connection = client or KoladaClient(provider)
    try:
        listings = {"municipality": list(load_municipalities().values())}
        for name in ("kpi", *LISTINGS):
            listings[name] = await connection.listing(name)
            logger.info("Discovered %d %s rows", len(listings[name]), name)
        return listings
    finally:
        if client is None:
            await connection.close()


def catalogue(rows, known: int) -> dict:
    """Validated KPI catalogue; a truncated listing must not look like removals."""
    native = {}
    for row in rows:
        identifier = row.get("id")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(row.get("title"), str)
            or not row["title"]
            or identifier in native
        ):
            raise ValueError(f"Malformed or duplicate KPI: {identifier!r}")
        native[identifier] = row
    if known and len(native) < known / 2:
        raise ValueError(f"KPI catalogue lists {len(native)} of {known} known KPIs")
    return native


def scan_start(refreshed: str | None) -> str | None:
    """Inclusive one-day overlap protects same-day updates."""
    if not refreshed:
        return None
    return (date.fromisoformat(refreshed[:10]) - timedelta(days=1)).isoformat()


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
    """Refresh accepted Kolada metadata from the change feed.

    The KPI/scope restriction bounds the rebuild work; the refresh marker only
    advances after an unrestricted, complete change scan. ``resume`` is accepted
    for the shared CLI: a run always finishes or leaves pending work for the next.
    """
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    if not (data / STATE).exists():
        raise ValueError("Kolada current state is missing from the checkout")
    baseline = manifest(data)
    state, documents = load_state(data), load_documents(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, dest)
    today = (clock or datetime.now(UTC)).date()
    run_id = uuid4().hex
    selected = set(kpis) if kpis else None
    restricted = selected is not None or scope is not None
    logger.info(
        "Kolada refresh %s: kpis=%s scope=%s refreshed=%s; %d KPIs, %d scopes, "
        "%d pending, %d accepted documents",
        run_id,
        sorted(selected) if selected else "all",
        scope or "all",
        state["refreshed"],
        len(state["kpis"]),
        len(state["scopes"]),
        len(state["pending"]),
        len(documents),
    )
    connection = client or KoladaClient(provider)
    if isinstance(connection, KoladaClient):
        connection.deadline = monotonic() + budget_seconds
    progress = Progress("kolada", "kolada")
    progress.update(phase="discovery", run_id=run_id, wave=1)
    counts = {"updated": 0, "removed": 0, "failed": 0, "reused": 0, "checked": 0}
    errors = []
    incomplete = False

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

    def drop(code):
        state["scopes"].pop(code, None)
        state["pending"].pop(code, None)
        if documents.pop(code, None) is not None:
            counts["removed"] += 1

    try:
        progress.update(current="kpi", operation="catalogue")
        try:
            native = catalogue(await connection.listing("kpi"), len(state["kpis"]))
        except BudgetExpired:
            raise
        except Exception as error:
            fail("discovery", "kpi", error)
            native = None
        if native is not None:
            for identifier in sorted(set(state["kpis"]) - set(native)):
                for kind in SCOPES:
                    if chosen(f"{kind}/{identifier}"):
                        drop(f"{kind}/{identifier}")
                if not any(c.endswith("/" + identifier) for c in state["scopes"]):
                    state["kpis"].pop(identifier)
                    logger.info("KPI %s left the catalogue; removed", identifier)
            state["kpis"].update(native)
        state["references"]["municipality"] = load_municipalities()
        for name in LISTINGS:
            try:
                progress.update(current=name, operation="catalogue")
                rows = await connection.listing(name)
                if any(
                    not isinstance(row.get("id"), str) or not row["id"] for row in rows
                ) or len({r["id"] for r in rows}) != len(rows):
                    raise ValueError(f"Malformed/duplicate {name} catalogue")
                state["references"][name] = reference_rows(rows)
            except BudgetExpired:
                raise
            except Exception as error:
                fail("discovery", name, error)
                state["references"].setdefault(name, {})
        references = state["references"]
        inventory = set(references["municipality"])

        # Change scan: additions fold into coverage, deletions force a rescan.
        progress.update(phase="changes")
        scanned = {}
        from_date = scan_start(state["refreshed"])
        for kind in SCOPES:
            if scope not in (None, kind) or native is None or from_date is None:
                continue
            if kind == "municipality":
                ids = sorted(inventory)
            else:
                ids = sorted(i for i, k in native.items() if k.get("has_ou_data"))
                if selected is not None:
                    ids = [i for i in ids if i in selected]
            if not ids:
                continue
            try:
                progress.update(current=kind, operation="change scan")
                records = await connection.changed_records(
                    kind, ids, years_for(kind, today), from_date
                )
            except BudgetExpired:
                raise
            except Exception as error:
                fail("changes", kind, error)
                continue
            scanned[kind] = defaultdict(list)
            for record in records:
                scanned[kind][record["kpi"]].append(record)
        for kind, by_kpi in scanned.items():
            for identifier, records in by_kpi.items():
                code = f"{kind}/{identifier}"
                if identifier not in native or not chosen(code):
                    continue
                entry = state["scopes"].setdefault(
                    code, {"coverage": {}, "empty": False}
                )
                if any(r["deleted"] for r in records):
                    state["pending"][code] = "rescan"
                    continue
                entry["coverage"] = cov.merge(
                    kind, entry["coverage"] or None, records, entities=inventory
                )
                entry["empty"] = False
                state["pending"].setdefault(code, "rebuild")
        complete_scan = native is not None and all(k in scanned for k in SCOPES)

        # Queue scopes that are new, undocumented or whose metadata went stale.
        for identifier, kpi in sorted(state["kpis"].items()):
            for kind in SCOPES:
                code = f"{kind}/{identifier}"
                if not chosen(code):
                    continue
                if kind in scanned:
                    counts["checked"] += 1
                entry = state["scopes"].get(code)
                if entry is None:
                    if kind == "ou" and not kpi.get("has_ou_data"):
                        continue
                    state["scopes"][code] = {"coverage": {}, "empty": False}
                    state["pending"][code] = "rescan"
                    continue
                document = documents.get(code)
                if document is None:
                    if not entry["empty"] and code not in state["pending"]:
                        state["pending"][code] = (
                            "rebuild" if entry["coverage"].get("years") else "rescan"
                        )
                    continue
                saved = document["extension"]["kolada"]
                context = context_for(kpi, kind, entry["coverage"], references)
                if saved["source_record"] != kpi or saved.get(
                    "references_digest"
                ) != context_digest(context):
                    state["pending"].setdefault(code, "rebuild")
        if complete_scan and not restricted:
            state["refreshed"] = today.isoformat()

        tasks = [code for code in sorted(state["pending"]) if chosen(code)]
        counts["reused"] = sum(chosen(c) and c not in tasks for c in documents)
        logger.info(
            "Rebuilding %d scopes (%d rescans); %d documents reused",
            len(tasks),
            sum(state["pending"][c] == "rescan" for c in tasks),
            counts["reused"],
        )
        progress.update(phase="metadata", total=len(tasks), processed=0)
        for position, code in enumerate(tasks):
            kind, identifier = code.split("/", 1)
            task = state["pending"][code]
            progress.update(current=code, operation=task)
            entry = state["scopes"][code]
            kpi = state["kpis"][identifier]
            try:
                if task == "rescan":
                    records = await connection.scope_records(
                        kind, identifier, years_for(kind, today)
                    )
                    entry["coverage"] = cov.rebuild(kind, records, entities=inventory)
                years = entry["coverage"].get("years") or []
                if years:
                    replacement = build_dataset(
                        kpi,
                        kind,
                        entry["coverage"],
                        references,
                        updated=updated_for(
                            kpi,
                            years[-1],
                            today,
                            references.get("portal_modified", {}).get(identifier),
                        ),
                    )
                    validate_metadata(replacement, adapter="kolada")
                    documents[code] = replacement
                    counts["updated"] += 1
                elif documents.pop(code, None) is not None:
                    counts["removed"] += 1
                entry["empty"] = not years
                state["pending"].pop(code)
            except BudgetExpired:
                raise
            except Exception as error:
                fail("metadata", code, error)
                counts["failed"] += 1
            progress.update(processed=position + 1, failed=counts["failed"])
    except BudgetExpired:
        incomplete = True
        logger.warning("Request budget expired; pending work waits for the next run")
    finally:
        if client is None:
            await connection.close()
    save(output, state, documents)
    status = (
        "completed_with_errors" if errors else "running" if incomplete else "success"
    )
    logger.info(
        "Run %s finished: status=%s counts=%s errors=%d pending=%d requests=%s",
        run_id,
        status,
        counts,
        len(errors),
        len(state["pending"]),
        getattr(connection, "requests", "n/a"),
    )
    result = {
        "run_id": run_id,
        "wave": 1,
        "status": status,
        "continue": False,
        "counts": counts,
        "errors": errors,
        "pending": len(state["pending"]),
        "refreshed": state["refreshed"],
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
    if manifest(data) != result["base_manifest"]:
        raise ValueError("Stale Kolada artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Kolada artifact checksum mismatch")
    if set(result["manifest"]) - {COLLECTION, STATE, "reports/latest.json"}:
        raise ValueError("Unexpected Kolada artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    (data / "work/current.json").unlink(missing_ok=True)
    return result

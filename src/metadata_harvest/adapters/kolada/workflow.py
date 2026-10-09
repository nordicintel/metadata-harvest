"""Incremental orchestration with independent KPI/scope failures."""

import shutil
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from metadata_harvest.adapters.kolada.client import BudgetExpired, KoladaClient, chunks
from metadata_harvest.adapters.kolada.metadata import (
    build_dataset,
    context_digest,
    context_for,
    reference_rows,
)
from metadata_harvest.adapters.kolada.state import (
    COLLECTION,
    STATE,
    load_documents,
    load_state,
    save,
)
from metadata_harvest.artifacts import check_collection_sizes, manifest
from metadata_harvest.progress import Progress
from metadata_harvest.storage import read, write
from metadata_harvest.validation import validate_metadata


async def discover_provider(provider: dict, *, client=None) -> dict:
    """Return fully paginated native listings using an injectable connection."""
    connection = client or KoladaClient(provider)
    try:
        return {
            name: await connection.listing(name)
            for name in (
                "kpi",
                "municipality",
                "ou",
                "municipality_groups",
                "kpi_groups",
            )
        }
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
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        dest = output / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, dest)
    current = data / "work/current.json"
    previous = read(current) if resume and current.exists() else {}
    if previous.get("selection") and kpis is None and scope is None:
        kpis = previous["selection"]["kpis"]
        scope = previous["selection"]["scope"]
    run_id = previous.get("run_id", uuid4().hex)
    wave = previous.get("wave", 0) + 1
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
        progress.error(stage, resource, "sv", entry["error"])

    def persist():
        nonlocal saved_changes
        changes = counts["updated"] + counts["removed"]
        save(output, state, documents, write_metadata=changes != saved_changes)
        saved_changes = changes
        checkpoint = output / "work/current.json"
        if state["pending"] or incomplete:
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
            checkpoint.unlink(missing_ok=True)

    try:
        complete = True
        try:
            progress.update(current="kpi", operation="catalogue")
            catalogue = await connection.listing("kpi")
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
        except Exception as error:
            if isinstance(error, BudgetExpired):
                raise
            fail("discovery", "kpi", error)
            native, complete = {}, False
        if complete:
            for identifier in sorted(set(state["kpis"]) - set(native)):
                if selected is not None and identifier not in selected:
                    continue
                try:
                    progress.update(current=identifier, operation="confirm removal")
                    row = await connection.kpi(identifier)
                    if row is None:
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
                        native[identifier] = row
                except Exception as error:
                    if isinstance(error, BudgetExpired):
                        raise
                    fail("discovery", identifier, error)
        state["kpis"].update(native)
        for name in ("municipality", "ou", "municipality_groups", "kpi_groups"):
            try:
                progress.update(current=name, operation="catalogue")
                rows = await connection.listing(name)
                if any(
                    not isinstance(row.get("id"), str) or not row["id"] for row in rows
                ) or len({r["id"] for r in rows}) != len(rows):
                    raise ValueError(f"Malformed/duplicate {name} catalogue")
                state["references"][name] = reference_rows(rows)
            except Exception as error:
                if isinstance(error, BudgetExpired):
                    raise
                fail("discovery", name, error)

        batches = defaultdict(list)
        for identifier, kpi in sorted(state["kpis"].items()):
            for kind in ("municipality", "ou"):
                code = f"{kind}/{identifier}"
                if not chosen(code) or (
                    kind == "ou"
                    and not kpi.get("has_ou_data")
                    and code not in state["scopes"]
                ):
                    continue
                years = list(range(1994 if kind == "ou" else 1970, started.year + 6))
                entry = state["scopes"].get(code)
                if entry is None:
                    entry = {"coverage": {}, "periods": years}
                    state["scopes"][code] = entry
                    state["pending"][code] = "data"
                document = documents.get(code)
                if document is None and not entry.get("empty"):
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
                    continue
                batches[(kind, cutoff, tuple(years))].append(identifier)
        progress.update(
            phase="changes", total=sum(map(len, batches.values())), processed=0
        )
        for (kind, cutoff, periods), identifiers in batches.items():
            for batch in chunks(identifiers, 25):
                if monotonic() >= deadline:
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
                            for identifier in ids:
                                await check([identifier])
                        else:
                            fail("changes", f"{kind}/{ids[0]}", error)
                        return
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
        persist()
        tasks = [code for code in sorted(state["pending"]) if chosen(code)]
        counts["reused"] = sum(chosen(c) and c not in tasks for c in documents)
        progress.update(
            phase="metadata", total=len(tasks), processed=0, reused=counts["reused"]
        )
        for position, code in enumerate(tasks):
            if monotonic() >= deadline:
                incomplete = True
                break
            kind, identifier = code.split("/", 1)
            progress.update(current=code, operation=state["pending"][code])
            entry = state["scopes"][code]
            try:
                coverage = entry["coverage"]
                modified = entry.get("modified")
                if state["pending"][code] == "data":
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
                else:
                    if documents.pop(code, None) is not None:
                        counts["removed"] += 1
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
            progress.update(
                processed=position + 1,
                succeeded=counts["updated"],
                failed=counts["failed"],
            )
            if (position + 1) % 25 == 0:
                persist()
    except BudgetExpired:
        incomplete = True
    finally:
        if client is None:
            await connection.close()
    persist()
    status = (
        "completed_with_errors" if errors else "running" if incomplete else "success"
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
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    if "work/current.json" not in result["manifest"]:
        (data / "work/current.json").unlink(missing_ok=True)
    return result

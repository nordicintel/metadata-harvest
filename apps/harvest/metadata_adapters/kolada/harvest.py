"""Incremental Kolada refresh: one change scan, then per-KPI rebuilds or rescans.

Kolada does not fit the fetch-and-settle loop: every run lists the KPI catalogue
and reference tables, scans all cells added or deleted since the last completed
refresh, folds additions into saved coverage and rescans KPIs with deletions from
scratch under a request budget. Only affected KPI scopes are refreshed; failures
stay pending for the next run and never erase stored data. Each scope's envelope
holds its KPI row and, as derived data, the coverage distilled from the value
cells (which are never stored) and the dates the mapping needs; the four listings
are stored as shared source files. The adapter keeps this loop and uses the
lifecycle's disk-handling half.
"""

import gzip
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from time import monotonic

from apps.harvest.metadata_adapters.kolada import coverage as cov
from apps.harvest.metadata_adapters.kolada.client import (
    PROVIDER_CODE,
    BudgetExpired,
    KoladaClient,
)
from apps.harvest.metadata_adapters.kolada.mapping import (
    context_digest,
    context_for,
    load_municipalities,
    reference_rows,
    updated_for,
    years_for,
)
from apps.harvest.metadata_adapters.kolada.parse import stored_key
from harvest import lifecycle
from harvest.lifecycle import Adapter, Run
from registry import ServiceBinding

SCOPES = ("municipality", "ou")
LISTINGS = ("ou", "municipality_groups", "kpi_groups")
EMPTY_STATE = {"kpis": {}, "references": {}, "scopes": {}, "pending": {}}
TASKS = {"data": "rescan", "metadata": "rebuild"}


def normalize(state: dict) -> dict:
    """Reduce earlier per-scope probe markers to coverage plus a refresh date."""
    state = {**EMPTY_STATE, **state}
    stamps = []
    for code, entry in list(state["scopes"].items()):
        for key in ("checked_at", "data_checked_at", "coverage_at"):
            if entry.get(key):
                stamps.append(entry[key][:10])
        state["scopes"][code] = {
            "coverage": entry.get("coverage") or {},
            "empty": bool(entry.get("empty")),
        }
    if "refreshed" not in state:
        state["refreshed"] = min(stamps) if stamps else None
    state["pending"] = {
        code: TASKS.get(task, task) for code, task in state["pending"].items()
    }
    return state


def load_state(root: Path) -> dict:
    path = Path(root) / lifecycle.STATE
    if not path.exists():
        return normalize({})
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return normalize(json.loads(stream.readline()))


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
    binding: ServiceBinding,
    data: Path,
    output: Path,
    *,
    selection: dict | None = None,
    client=None,
    clock=None,
) -> dict:
    """Refresh stored Kolada metadata from the change feed.

    ``selection`` may hold ``kpis`` (KPI ids), ``scope`` (``municipality`` or
    ``ou``) and ``budget_seconds``. A KPI/scope restriction bounds the rebuild work;
    the refresh marker only advances after an unrestricted, complete change scan.
    A run always finishes or leaves pending work for the next scheduled run.
    """
    this: Run = lifecycle.prepare(
        ADAPTER, binding, data, output, selection=selection, client=client, clock=clock
    )
    this.state = normalize(this.state)
    state, store, connection = this.state, this.store, this.client
    today = this.now.date()
    selected = set(this.selection.get("kpis") or ()) or None
    scope = this.selection.get("scope")
    restricted = selected is not None or scope is not None
    if isinstance(connection, KoladaClient):
        connection.deadline = monotonic() + (
            this.selection.get("budget_seconds") or 19_200
        )
    this.counts = {"updated": 0, "removed": 0, "failed": 0, "reused": 0, "checked": 0}
    counts = this.counts
    incomplete = False
    this.log.info(
        "Kolada refresh: kpis=%s scope=%s refreshed=%s; %d KPIs, %d scopes, "
        "%d pending, %d stored scopes",
        sorted(selected) if selected else "all",
        scope or "all",
        state["refreshed"],
        len(state["kpis"]),
        len(state["scopes"]),
        len(state["pending"]),
        len(store.entries()),
    )

    def chosen(code):
        kind, identifier = code.split("/", 1)
        return (selected is None or identifier in selected) and (
            scope is None or kind == scope
        )

    def drop(code):
        state["scopes"].pop(code, None)
        state["pending"].pop(code, None)
        if store.get(stored_key(code)) is not None:
            store.remove(stored_key(code))
            counts["removed"] += 1

    try:
        this.progress.update(current="kpi", operation="catalogue")
        try:
            kpi_rows = await connection.listing("kpi")
            native = catalogue(kpi_rows, len(state["kpis"]))
            store.put_shared(
                PROVIDER_CODE, "kpi.json", kpi_rows, retrieved_at=this.stamp
            )
        except BudgetExpired:
            raise
        except Exception as error:
            this.fail("discovery", "kpi", error)
            native = None
        if native is not None:
            for identifier in sorted(set(state["kpis"]) - set(native)):
                for kind in SCOPES:
                    if chosen(f"{kind}/{identifier}"):
                        drop(f"{kind}/{identifier}")
                if not any(c.endswith("/" + identifier) for c in state["scopes"]):
                    state["kpis"].pop(identifier)
            state["kpis"].update(native)
        state["references"]["municipality"] = load_municipalities()
        for name in LISTINGS:
            try:
                this.progress.update(current=name, operation="catalogue")
                rows = await connection.listing(name)
                if any(
                    not isinstance(row.get("id"), str) or not row["id"] for row in rows
                ) or len({r["id"] for r in rows}) != len(rows):
                    raise ValueError(f"Malformed/duplicate {name} catalogue")
                state["references"][name] = reference_rows(rows)
                if rows:
                    store.put_shared(
                        PROVIDER_CODE, f"{name}.json", rows, retrieved_at=this.stamp
                    )
            except BudgetExpired:
                raise
            except Exception as error:
                this.fail("discovery", name, error)
                state["references"].setdefault(name, {})
        references = state["references"]
        inventory = set(references["municipality"])

        # Change scan: additions fold into coverage, deletions force a rescan.
        this.progress.update(phase="changes")
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
                this.progress.update(current=kind, operation="change scan")
                records = await connection.changed_records(
                    kind, ids, years_for(kind, today), from_date
                )
            except BudgetExpired:
                raise
            except Exception as error:
                this.fail("changes", kind, error)
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
                envelope = store.get(stored_key(code))
                if envelope is None:
                    if not entry["empty"] and code not in state["pending"]:
                        state["pending"][code] = (
                            "rebuild" if entry["coverage"].get("years") else "rescan"
                        )
                    continue
                context = context_for(kpi, kind, entry["coverage"], references)
                if envelope["responses"]["kpi"] != kpi or envelope["derived"].get(
                    "references_digest"
                ) != context_digest(context):
                    state["pending"].setdefault(code, "rebuild")
        if complete_scan and not restricted:
            state["refreshed"] = today.isoformat()

        tasks = [code for code in sorted(state["pending"]) if chosen(code)]
        stored_codes = [
            key.split("/", 1)[1].rsplit("/", 1)[0] for key in store.entries()
        ]
        counts["reused"] = sum(chosen(c) and c not in tasks for c in stored_codes)
        this.log.info(
            "Refreshing %d scopes (%d rescans); %d stored scopes reused",
            len(tasks),
            sum(state["pending"][c] == "rescan" for c in tasks),
            counts["reused"],
        )
        this.progress.update(phase="metadata", total=len(tasks), processed=0)
        for position, code in enumerate(tasks):
            kind, identifier = code.split("/", 1)
            task = state["pending"][code]
            this.progress.update(current=code, operation=task)
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
                    context = context_for(kpi, kind, entry["coverage"], references)
                    store.put(
                        stored_key(code),
                        responses={"kpi": kpi},
                        derived={
                            "coverage": entry["coverage"],
                            "updated": updated_for(kpi, years[-1], today),
                            "references_digest": context_digest(context),
                        },
                        updated=kpi.get("publication_date"),
                        retrieved_at=this.stamp,
                    )
                    counts["updated"] += 1
                elif store.get(stored_key(code)) is not None:
                    store.remove(stored_key(code))
                    counts["removed"] += 1
                entry["empty"] = not years
                state["pending"].pop(code)
            except BudgetExpired:
                raise
            except Exception as error:
                this.fail("metadata", code, error)
                counts["failed"] += 1
            this.progress.update(processed=position + 1, failed=counts["failed"])
    except BudgetExpired:
        incomplete = True
        this.log.warning("Request budget expired; pending work waits for the next run")
    finally:
        if client is None:
            await connection.close()
    status = (
        "completed_with_errors"
        if this.errors
        else "running"
        if incomplete
        else "success"
    )
    return lifecycle.finish(
        this, status=status, pending=len(state["pending"]), refreshed=state["refreshed"]
    )


ADAPTER = Adapter(
    name="kolada",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-kolada.yml",
    client=lambda binding: KoladaClient(binding.options, base_url=binding.endpoint_url),
    run=run,
    options={"kpi": "kpis", "scope": "scope", "budget-seconds": "budget_seconds"},
    languages=("sv",),
    collection="scopes",
    scope_failures=lambda report: [
        e["resource"]
        for e in report.get("errors") or []
        if e.get("stage") == "discovery" and e.get("resource") == "kpi"
    ],
)

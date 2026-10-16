"""The loop around a harvest run, owned once for every adapter.

An adapter describes itself with an :class:`Adapter`: what it is called, how to
discover what the provider lists (scopes with their tasks) and how to fetch one
task. The lifecycle owns everything else: the separate worker output, the baseline
copy, the source store, state, the retry after a cooldown, published or withheld,
reports, ``result.json`` and ``collect``.

Three parts are exposed. :func:`run` is the fetch-and-settle loop. :func:`prepare`
and :func:`finish` are its disk-handling half, which adapters with a loop of their
own use directly. :func:`collect` applies a worker output to the published data.

A **key** is the identity of one stored item, ``<language>/<code>``. A **scope** is
one unit of discovery (a product, an API, a listing); its tasks are the keys it
lists. A scope that cannot be loaded keeps its previously known keys; a complete
scope in an unbounded run removes the keys it no longer lists.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.source_store import (
    Rejected,
    SourceStore,
    harvest_manifest,
    source_key,
    split_key,
)
from apps.harvest.storage import read, write, write_jsonl
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest
from registry import ServiceBinding

STATE = "state/current.jsonl.gz"
REPORT = "reports/latest.json"
COUNTS = ("rebuilt", "unchanged", "failed", "withheld", "restored", "removed")


class Unreachable(Exception):
    """The provider's endpoint did not answer; the item is retried once, then withheld."""


@dataclass
class Task:
    """One item a scope lists: its key and whatever the adapter needs to fetch it."""

    key: str
    item: object = None


@dataclass
class Scope:
    """One unit of discovery.

    ``load`` lists the scope's tasks; it may store shared source files through the
    run. Raising :class:`Unreachable` is an endpoint failure (retried once), any
    other exception a metadata failure. ``complete`` says whether the listing is
    complete, which is what allows removals.
    """

    name: str
    load: Callable[[], Awaitable[list[Task]]]
    complete: bool = True


@dataclass
class Stored:
    """What a fetched task stores: the provider's responses, exactly as received."""

    responses: dict
    derived: dict | None = None
    updated: object = None
    files: dict | None = None
    info: dict = field(default_factory=dict)
    """Extra fields recorded in the key's state entry."""


@dataclass
class Kept:
    """The stored entry is unchanged by the adapter's own change signal; nothing
    is written and the item counts as unchanged."""

    info: dict = field(default_factory=dict)


@dataclass
class Dropped:
    """The task has no item to store; the key and its files are removed."""

    count: str
    """The result counter to increase, for example ``untranslated``."""


@dataclass(frozen=True)
class Adapter:
    name: str
    provider_code: str
    workflow_file: str
    client: Callable[[ServiceBinding], object]
    discover: Callable[[Run], Awaitable[list[Scope]]]
    fetch: Callable[[Run, Task], Awaitable[Stored | Kept | Dropped]]
    options: dict[str, str] = field(default_factory=dict)
    """CLI flag to selection name, for example ``{"subject": "subjects"}``."""
    languages: tuple[str, ...] = ("sv",)
    collection: str = "datasets"
    counts: tuple[str, ...] = ()
    """Result counters beyond the lifecycle's own."""
    paths: tuple[str, ...] = ()
    """Extra files or prefixes a harvest may write besides state, report and source."""
    forget: Callable[[Run, str], None] | None = None
    """Drop the companion files of a removed key once nothing else needs them."""
    scope_of: Callable[[str], str] = lambda key: "*"
    """The scope name a known key belongs to; ``*`` means every scope."""
    scope_failures: Callable[[dict], list[str]] = lambda report: [
        e["resource"]
        for e in report.get("errors") or []
        if str(e.get("resource")).startswith("*/")
        and "Unknown" not in str(e.get("error"))
    ]
    """Whole scopes that failed in a run, read from its report."""


@dataclass
class Run:
    """Everything a run holds; adapters read from it and store through it."""

    adapter: Adapter
    binding: ServiceBinding
    data: Path
    output: Path
    client: object
    store: SourceStore
    state: dict
    baseline: dict
    now: datetime
    languages: list[str]
    selection: dict
    log: logging.Logger
    progress: Progress
    counts: dict
    errors: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    published: dict = field(default_factory=dict)
    withheld: dict = field(default_factory=dict)

    @property
    def stamp(self) -> str:
        return self.now.isoformat()

    @property
    def records(self) -> dict:
        return self.state[self.adapter.collection]

    @property
    def bounded(self) -> bool:
        """Whether the run was restricted to named items (no removals then)."""
        return any(
            value
            for name, value in self.selection.items()
            if name not in ("languages", "full")
        )

    def stored_key(self, key: str) -> str:
        language, code = key.split("/", 1)
        return source_key(self.adapter.provider_code, code, language)

    def known(self, scope: str | None = None) -> set[str]:
        """Keys known before this run, in the selected languages (and scope)."""
        keys = set(self.published) | set(self.withheld) | set(self.records)
        return {
            key
            for key in keys
            if key.split("/", 1)[0] in self.languages
            and (scope is None or self.adapter.scope_of(key) in (scope, "*"))
        }

    def fail(self, stage: str, resource: str, error: Exception, **details) -> dict:
        """Record one failure in the report and the progress file."""
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        self.errors.append(entry)
        self.log.error("%s failure for %s: %s", stage, resource, entry["error"])
        self.progress.error(
            stage, resource, resource.split("/", 1)[0], entry["error"], **details
        )
        return entry

    def remove(self, key: str) -> None:
        """Forget a key: its state, its source entry and its companions."""
        self.records.pop(key, None)
        self.store.remove(self.stored_key(key))
        if (self.published.pop(key, None) or self.withheld.pop(key, None)) is not None:
            self.counts["removed"] += 1
        if self.adapter.forget is not None:
            self.adapter.forget(self, key)


def _normal(value):
    """The value as it reads back from JSON (tuples become lists)."""
    return json.loads(json.dumps(value, ensure_ascii=False))


def state_key(stored: str) -> str:
    _, code, language = split_key(stored)
    return f"{language}/{code}"


def load_state(root: Path, collection: str) -> dict:
    path = Path(root) / STATE
    if not path.exists():
        return {collection: {}}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.loads(stream.readline())
    state.setdefault(collection, {})
    return state


def allowed_path(adapter: Adapter, relative: str) -> bool:
    return (
        relative in {STATE, REPORT}
        or relative.startswith("source/")
        or any(relative == p or relative.startswith(p) for p in adapter.paths)
    )


def prepare(
    adapter: Adapter,
    binding: ServiceBinding,
    data: Path,
    output: Path,
    *,
    selection: dict | None = None,
    client=None,
    clock: datetime | None = None,
) -> Run:
    """Check the directories, copy the baseline, open the store and load state."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    selection = dict(selection or {})
    configured = list(binding.options.get("languages", adapter.languages))
    if set(configured) - set(adapter.languages):
        raise ValueError(f"{adapter.name} does not publish {configured}")
    languages = list(selection.get("languages") or configured)
    if set(languages) - set(configured):
        raise ValueError(f"Unconfigured {adapter.name} languages: {languages}")
    selection["languages"] = languages
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not allowed_path(adapter, relative) for relative in baseline):
        raise ValueError(f"Unexpected file in published {adapter.name} data")
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    log = logging.getLogger(f"{adapter.name}_logger")
    run = Run(
        adapter=adapter,
        binding=binding,
        data=data,
        output=output,
        client=client or adapter.client(binding),
        store=SourceStore(output / "source", adapter.name),
        state=load_state(data, adapter.collection),
        baseline=baseline,
        now=clock or datetime.now(UTC),
        languages=languages,
        selection=selection,
        log=log,
        progress=Progress(adapter.name, adapter.provider_code),
        counts=dict.fromkeys((*COUNTS, *adapter.counts), 0),
    )
    # Placeholders stand in for documents: presence means a stored source entry.
    for stored in run.store.entries():
        key = state_key(stored)
        status = run.records.get(key, {}).get("status")
        (run.withheld if status == "withheld" else run.published)[key] = True
    log.info(
        "%s run starting: languages %s (configured %s); selection %s; baseline %d "
        "published, %d withheld, %d tracked, %d files",
        adapter.name,
        languages,
        configured,
        {k: v for k, v in selection.items() if k != "languages"} or "all",
        len(run.published),
        len(run.withheld),
        len(run.records),
        len(baseline),
    )
    return run


def finish(run: Run, *, status: str | None = None, **extra) -> dict:
    """Save the store and state, write the report and result.json."""
    run.store.save()
    write_jsonl(run.output / STATE, [run.state])
    status = status or report_status(pending=0, errors=run.errors)
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "selection": run.selection,
        "counts": run.counts,
        "published": {
            language: sum(k.startswith(f"{language}/") for k in run.published)
            for language in run.languages
        },
        "withheld": sorted(run.withheld),
        **run.report,
        **extra,
        "errors": run.errors,
    }
    write(run.output / REPORT, result)
    check_collection_sizes(run.output)
    result.update(base_manifest=run.baseline, manifest=manifest(run.output))
    write(run.output / "result.json", result)
    run.progress.update(phase="finished", result_status=status, current=None)
    run.log.info(
        "%s run finished with status %s: counts %s; published %s; %d withheld; "
        "%d errors",
        run.adapter.name,
        status,
        run.counts,
        result["published"],
        len(run.withheld),
        len(run.errors),
    )
    return result


async def _load_scope(run: Run, scope: Scope, failures: dict) -> list[Task] | None:
    try:
        tasks = await scope.load()
    except Exception as error:
        stage = "endpoint" if isinstance(error, Unreachable) else "metadata"
        failures[f"*/{scope.name}"] = (stage, error)
        run.log.warning(
            "Scope %s: discovery failed at the %s stage (%s: %s); its known items "
            "keep their stored files%s",
            scope.name,
            stage,
            type(error).__name__,
            error,
            "; will retry after the cooldown" if stage == "endpoint" else "",
        )
        return None
    failures.pop(f"*/{scope.name}", None)
    return [t for t in tasks if t.key.split("/", 1)[0] in run.languages]


async def _fetch(run: Run, task: Task, working: dict, failures: dict) -> str:
    """Fetch one task; return ``retry``, ``dropped`` or ``done``."""
    key = task.key
    info = run.records.setdefault(key, {})
    info["checked_at"] = run.stamp
    try:
        result = await run.adapter.fetch(run, task)
    except Unreachable as error:
        working[key], failures[key] = False, ("endpoint", error)
        run.log.warning("%s: endpoint unreachable (%s)", key, error)
        return "retry"
    except Exception as error:
        working[key], failures[key] = True, ("metadata", error)
        run.log.warning("%s: %s: %s", key, type(error).__name__, error)
        return "done"
    working[key] = True
    if isinstance(result, Kept):
        failures.pop(key, None)
        run.counts["unchanged"] += 1
        info.update(verified_at=run.stamp, error=None, **result.info)
        (run.withheld if key in run.withheld else run.published)[key] = True
        return "done"
    if isinstance(result, Dropped):
        run.counts[result.count] += 1
        run.records.pop(key, None)
        run.published.pop(key, None)
        run.withheld.pop(key, None)
        run.store.remove(run.stored_key(key))
        failures.pop(key, None)
        run.log.debug("%s: dropped (%s)", key, result.count)
        return "dropped"
    stored = run.store.get(run.stored_key(key))
    try:
        run.store.put(
            run.stored_key(key),
            responses=result.responses,
            derived=result.derived,
            updated=result.updated,
            retrieved_at=run.stamp,
            **({"files": result.files} if result.files else {}),
        )
    except Rejected as error:
        failures[key] = ("metadata", error)
        run.log.warning("%s: response not storable (%s)", key, error)
        return "done"
    failures.pop(key, None)
    unchanged = (
        stored is not None
        and stored["responses"] == _normal(result.responses)
        and (result.derived is None or stored.get("derived") == _normal(result.derived))
    )
    run.counts["unchanged" if unchanged else "rebuilt"] += 1
    info.update(verified_at=run.stamp, error=None, **result.info)
    (run.withheld if key in run.withheld else run.published)[key] = True
    return "done"


async def run(
    adapter: Adapter,
    binding: ServiceBinding,
    data: Path,
    output: Path,
    *,
    selection: dict | None = None,
    client=None,
    clock: datetime | None = None,
) -> dict:
    """Discover, fetch each item, retry once, settle, and write the worker output."""
    this = prepare(
        adapter, binding, data, output, selection=selection, client=client, clock=clock
    )
    max_age = timedelta(days=binding.options.get("max_age_days", 60))
    cooldown = binding.options.get("retry_cooldown_seconds", 60)
    working, failures, settled, listed = {}, {}, set(), {}
    try:
        this.progress.update(phase="discovery")
        scopes = await adapter.discover(this)
        pending: list[Task] = []
        retry_scopes: list[Scope] = []
        for scope in scopes:
            tasks = await _load_scope(this, scope, failures)
            if tasks is None:
                if failures[f"*/{scope.name}"][0] == "endpoint":
                    retry_scopes.append(scope)
                continue
            listed[scope.name] = (scope, {t.key for t in tasks})
            pending.extend(tasks)
        this.progress.update(phase="metadata", total=len(pending), processed=0)
        retry = []
        for number, task in enumerate(pending, start=1):
            settled.add(task.key)
            this.progress.update(current=task.key, operation="fetch")
            outcome = await _fetch(this, task, working, failures)
            if outcome == "retry":
                retry.append(task)
            elif outcome == "dropped":
                settled.discard(task.key)
            this.progress.update(processed=number, failed=len(failures))
        if retry or retry_scopes:
            this.log.warning(
                "Retrying %d items and %d scopes after a %ss cooldown",
                len(retry),
                len(retry_scopes),
                cooldown,
            )
            this.progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for scope in retry_scopes:
                tasks = await _load_scope(this, scope, failures)
                if tasks is not None:
                    listed[scope.name] = (scope, {t.key for t in tasks})
                    retry.extend(tasks)
            for task in retry:
                settled.add(task.key)
                this.progress.update(current=task.key, operation="retry")
                if await _fetch(this, task, working, failures) == "dropped":
                    settled.discard(task.key)
        for name, (scope, keys) in listed.items():
            if scope.complete and not this.bounded:
                for key in sorted(this.known(name) - keys):
                    settled.discard(key)
                    failures.pop(key, None)
                    this.remove(key)
                    this.log.debug("%s: removed, %s no longer lists it", key, name)
        if not this.bounded:  # keys of scopes the provider no longer lists at all
            names = {s.name for s in scopes} | {"*"}
            for key in sorted(this.known()):
                if adapter.scope_of(key) not in names:
                    settled.discard(key)
                    failures.pop(key, None)
                    this.remove(key)
                    this.log.debug("%s: removed, its scope is no longer listed", key)
        for resource, (stage, error) in sorted(failures.items()):
            entry = this.fail(stage, resource, error)
            this.counts["failed"] += 1
            if resource.startswith("*/"):
                for key in this.known(resource[2:]):
                    settled.add(key)
                    working.setdefault(key, stage == "metadata")
                    this.records.setdefault(key, {})["error"] = entry
            elif resource in this.records:
                this.records[resource]["error"] = entry
        this.progress.update(failed=this.counts["failed"])
        settle(
            sorted(settled),
            this.published,
            this.withheld,
            this.records,
            working,
            this.now,
            max_age,
            this.counts,
        )
        outcomes: dict[str, int] = {}
        for key in sorted(settled):
            info = this.records.get(key, {})
            outcome = info.get("withheld_reason") or info.get("status")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        this.log.info("Settled outcomes: %s", dict(sorted(outcomes.items(), key=str)))
    finally:
        if client is None and hasattr(this.client, "close"):
            await this.client.close()
    return finish(this)


def collect(adapter: Adapter, data: Path, output: Path) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    log = logging.getLogger(f"{adapter.name}_logger")
    result = read(output / "result.json")
    log.info(
        "Collecting %s artifact (run %s, status %s)",
        adapter.name,
        result.get("run_id"),
        result.get("status"),
    )
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError(f"Stale {adapter.name} artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError(f"{adapter.name} artifact checksum mismatch")
    if not {STATE, REPORT} <= set(result["manifest"]) or not all(
        allowed_path(adapter, relative) for relative in result["manifest"]
    ):
        raise ValueError(f"Unexpected {adapter.name} artifact path")
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
    log.info("Applied %d artifact files", len(result["manifest"]))
    return result

"""Monthly, incremental RowStore refresh with a hard request budget.

EntryScape hosts rate-limit us, so a run spends requests only where the
catalogue shows change: a table whose distribution and dataset ``modified``
markers equal those of its last harvest costs nothing; a table without markers
costs one ``_limit=1`` request to compare its row count; new, changed and
previously failed tables are harvested (source files first, then enumeration,
then a full scan, deferred beyond ``max_paging_requests``). One pacer spaces
every request across all hosts; the second HTTP 429 or an exhausted budget stops
the run, keeping every finished table for the next one. Kolada-like, the adapter
keeps this loop over many providers and uses the lifecycle's disk-handling half.

Each harvested table's envelope holds its discovery record and, as derived data,
the trimmed structure document the dataset rules read (the rows themselves are
never stored). Dataset grouping and mapping happen later in ``parse``.
"""

import gzip
import json
from collections import Counter
from pathlib import Path

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
from apps.harvest.metadata_adapters.entryscape.discovery import discover_provider
from apps.harvest.metadata_adapters.entryscape.parse import stored_key
from apps.harvest.metadata_adapters.entryscape.tables import table_document
from apps.harvest.storage import write_jsonl
from harvest import lifecycle
from harvest.lifecycle import Adapter, Run
from registry import ServiceBinding

PROVIDER_CODE = "entryscape"
TABLES = "state/tables.jsonl.gz"
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
MOVED_FIELDS = ("record", "document")
"""Fields earlier state rows carried; they now live in the source files."""


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


def table_key(record: dict) -> str:
    return f"{record['provider_code']}/{record['dataset_code']}"


def tables_key(key: str) -> dict:
    """A record-like identity from a ``<provider>/<dataset_code>`` table key."""
    provider_code, dataset_code = key.split("/", 1)
    return {"provider_code": provider_code, "dataset_code": dataset_code}


def load_tables(root: Path) -> dict:
    path = Path(root) / TABLES
    tables = {}
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                entry = json.loads(line)
                for field in MOVED_FIELDS:
                    entry.pop(field, None)
                tables[entry["key"]] = entry
    return tables


async def discover_scope(binding: ServiceBinding, client: Client) -> dict:
    """The provider's single-language discovery scope."""
    async for scope in discover_provider(binding, client=client):
        return scope
    raise ValueError("Discovery yielded no scope")


async def run(
    bindings: list[ServiceBinding],
    data: Path,
    output: Path,
    *,
    selection: dict | None = None,
    client=None,
    clock=None,
) -> dict:
    """Refresh changed tables of every (or each selected) provider.

    ``selection`` may hold ``selected`` (provider codes), ``max_requests``,
    ``max_paging_requests``, ``requests_per_second`` and ``preflight``. ``client``
    is a factory ``(base_url, pacer) -> Client``.
    """
    this: Run = lifecycle.prepare(
        ADAPTER,
        bindings[0],
        data,
        output,
        selection=selection,
        client=client,
        clock=clock,
    )
    options = this.selection
    max_requests = options.get("max_requests") or DEFAULT_MAX_REQUESTS
    max_paging_requests = options.get("max_paging_requests") or MAX_PAGING_REQUESTS
    requests_per_second = options.get("requests_per_second") or REQUESTS_PER_SECOND
    preflight = options.get("preflight", True)
    tables, current, store = load_tables(this.output), this.state, this.store
    current.setdefault("mapping_version", None)
    current.setdefault("dropped", [])
    factory = this.client
    pacer = Pacer(
        1 / requests_per_second, budget=max_requests, max_throttles=MAX_THROTTLES
    )
    this.counts = dict.fromkeys(
        ("unchanged", "harvested", "empty", "deferred", "removed", "failed"), 0
    )
    counts = this.counts
    deferred, requests = [], 0
    interrupted = None
    strategies: Counter = Counter()
    done = 0
    wanted = set(options.get("selected") or ()) or None
    chosen = [b for b in bindings if wanted is None or b.source_code in wanted]
    for code in sorted((wanted or set()) - {b.source_code for b in bindings}):
        this.fail("discovery", f"*/{code}", LookupError("Unknown provider"))
    this.log.info(
        "EntryScape run: %d of %d configured providers, max_requests=%d, "
        "max_paging_requests=%d, %d known tables",
        len(chosen),
        len(bindings),
        max_requests,
        max_paging_requests,
        len(tables),
    )

    async def call(base_url, work):
        nonlocal requests
        async with factory(base_url, pacer) as connection:
            try:
                return await work(connection)
            except CONNECTION_ERRORS as error:
                if unresolvable(error):
                    raise
                raise Blocked(f"{base_url}: {type(error).__name__}") from error
            except (RateLimited, BudgetExhausted) as error:
                raise Blocked(f"{base_url}: {error}") from error
            finally:
                requests += connection.requests

    async def harvest(record: dict) -> str:
        key = table_key(record)
        entry = {
            "key": key,
            "provider_code": record["provider_code"],
            "dataset_code": record["dataset_code"],
            "markers": markers(record),
            "harvested_at": this.stamp,
        }
        try:
            structure = await call(
                record["base_url"],
                lambda connection: infer_structure(
                    connection,
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
        else:
            store.put(
                stored_key(record),
                responses={"discovery": record},
                derived={"document": trim(table_document(record, structure))},
                updated=record.get("published"),
                retrieved_at=this.stamp,
            )
            entry.update(
                status="ok",
                rowcount=structure["rowcount"],
                strategy=structure["strategy"],
                requests=structure["requests"],
            )
            counts["harvested"] += 1
            strategies[structure["strategy"]] += 1
        tables[key] = entry
        return entry["status"]

    try:
        if preflight:
            this.progress.update(phase="discovery", current="preflight")
            for host in PREFLIGHT_HOSTS:
                try:
                    await call(
                        host,
                        lambda connection: connection.get(
                            "store/search",
                            {"type": "solr", "query": "rdfType:x", "limit": 1},
                        ),
                    )
                except Exception as error:
                    raise Blocked(f"{host} is not reachable; not starting") from error
        table_errors = 0
        this.progress.update(phase="metadata", total=len(chosen), processed=0)
        for position, binding in enumerate(chosen, start=1):
            code = binding.source_code
            this.progress.update(current=code, operation="discovery")
            before = dict(counts)
            try:
                scope = await call(
                    binding.endpoint_url,
                    lambda connection, binding=binding: discover_scope(
                        binding, connection
                    ),
                )
            except Blocked:
                raise
            except Exception as error:
                this.fail("discovery", code, error)
                current["providers"][code] = {
                    "checked_at": this.stamp,
                    "complete": False,
                }
                continue
            if scope["errors"]:
                for text in scope["errors"]:
                    this.fail("discovery", code, RuntimeError(text))
                if any("Connect" in e and not unresolvable(e) for e in scope["errors"]):
                    raise Blocked(f"{binding.endpoint_url}: {scope['errors']}")
            current["providers"][code] = {
                "checked_at": this.stamp,
                "complete": scope["complete"],
                "tables": len(scope["records"]),
            }
            listed = {table_key(r): r for r in scope["records"]}
            if scope["complete"] and wanted is None:
                for key in [
                    k for k in tables if k.startswith(code + "/") and k not in listed
                ]:
                    tables.pop(key)
                    store.remove(stored_key(tables_key(key)))
                    counts["removed"] += 1
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
                if settled and any(markers(record).values()):
                    if previous["markers"] == markers(record):
                        counts["unchanged"] += 1
                        continue
                elif settled:
                    this.progress.update(current=key, operation="row count")
                    page = await call(
                        record["base_url"],
                        lambda connection, record=record: connection.get(
                            record["table_url"], {"_limit": 1}
                        ),
                    )
                    if page.get("resultCount") == previous.get("rowcount"):
                        previous["markers"] = markers(record)
                        counts["unchanged"] += 1
                        continue
                this.progress.update(current=key, operation="harvest")
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
                        "harvested_at": this.stamp,
                        "status": "error",
                        "error": this.fail("metadata", key, error),
                    }
                    counts["failed"] += 1
                    table_errors += 1
                    if table_errors >= MAX_TABLE_ERRORS:
                        raise Blocked(
                            f"{table_errors} unexpected table failures"
                        ) from error
            done += 1
            this.log.info(
                "Provider %s done: %d listed; %s; %d/%d requests spent",
                code,
                len(listed),
                ", ".join(
                    f"{counts[k] - before[k]} {k}"
                    for k in ("harvested", "empty", "deferred", "failed")
                    if counts[k] - before[k]
                )
                or "no table work",
                pacer.sent,
                max_requests,
            )
            this.progress.update(processed=position, succeeded=counts["harvested"])
    except Blocked as error:
        interrupted = str(error)
        this.log.error(
            "Run stopped early after %d of %d providers: %s; finished tables are "
            "kept and the rest waits for the next run",
            done,
            len(chosen),
            interrupted,
        )
        this.fail("run", "budget", error)
    this.progress.update(failed=counts["failed"])
    current.update(mapping_version=MAPPING_VERSION)
    write_jsonl(this.output / TABLES, (tables[k] for k in sorted(tables)))
    return lifecycle.finish(
        this,
        interrupted=interrupted,
        requests=requests,
        deferred_tables=deferred,
        strategies=dict(strategies),
    )


ADAPTER = Adapter(
    name="entryscape",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-entryscape.yml",
    client=lambda binding: default_client,
    run=run,
    options={"provider-code": "selected", "max-requests": "max_requests"},
    languages=("sv",),
    collection="providers",
    paths=(TABLES,),
    many=True,
    scope_failures=lambda report: (
        [
            e["resource"]
            for e in report.get("errors") or []
            if e.get("stage") == "discovery" and "Unknown" not in str(e.get("error"))
        ]
        + (
            [f"run: {report['interrupted']}"]
            if report.get("interrupted") and "budget" not in report["interrupted"]
            else []
        )
    ),
)

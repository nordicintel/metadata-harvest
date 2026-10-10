"""Monthly, incremental RowStore refresh with a hard request budget.

EntryScape hosts rate-limit us, so a run spends requests only where the
catalogue shows change: a table whose distribution and dataset ``modified``
markers equal those of its last harvest costs nothing; a table without markers
costs one ``_limit=1`` request to compare its row count; new, changed and
previously failed tables are harvested (source files first, then enumeration,
then a full scan, deferred beyond ``max_paging_requests``). One pacer spaces
every request across all hosts; the second HTTP 429 or an exhausted budget
stops the run, keeping every finished table for the next one.

Publication is offline: the dataset rules regroup every accepted table structure
on each run, so nothing is requested to apply a rule change.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from apps.harvest.metadata_adapters.entrysoft.categories import (
    EmptyTable,
    NeedsPaging,
    infer_structure,
)
from apps.harvest.metadata_adapters.entrysoft.client import (
    BudgetExhausted,
    Client,
    Pacer,
    RateLimited,
)
from apps.harvest.metadata_adapters.entrysoft.datasets import (
    MAPPING_VERSION,
    build_datasets,
    trim,
)
from apps.harvest.metadata_adapters.entrysoft.discovery import (
    discover_provider as discover,
)
from apps.harvest.metadata_adapters.entrysoft.discovery import within_catalog_scope
from apps.harvest.metadata_adapters.entrysoft.mapping import to_document
from apps.harvest.metadata_adapters.entrysoft.state import (
    CURRENT,
    REPORT,
    TABLES,
    collection_store,
    harvested_documents,
    load_current,
    load_tables,
    save_current,
    save_tables,
    table_key,
)
from apps.harvest.metadata_adapters.entrysoft.tables import table_document
from apps.harvest.storage import document_identity, filename, read, write
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest

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
    "datasets",
    "lists",
)


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


def rebuild(tables: dict, providers: list[dict]) -> tuple[dict, dict]:
    """Group every accepted table structure into datasets and map them.

    Returns the documents keyed by collection filename and a summary with the
    dropped tables and list-only datasets.
    """
    by_code = {p["code"]: p for p in providers}
    documents = harvested_documents(tables)
    datasets, dropped = build_datasets(documents)
    by_url = {d["extension"]["metadata_harvest"]["table_url"]: d for d in documents}
    mapped, lists = {}, []
    for dataset in datasets:
        members = [by_url[url] for url in dataset["tables"]]
        dataset["records"] = [m["extension"]["metadata_harvest"] for m in members]
        dataset["cells_unique"] = all(
            m["extension"]["rowstore"].get("dimension_key_unique_in_sample", False)
            for m in members
        )
        if dataset["kind"] != "cube":
            lists.append(dataset["dataset_code"])
            continue
        document = to_document(dataset, by_code.get(dataset["provider_code"], {}))
        mapped[filename(document_identity(document))] = document
    return mapped, {"dropped": dropped, "lists": lists, "datasets": len(mapped)}


def publish(tables: dict, current: dict, providers: list[dict], output: Path) -> dict:
    """Write the derived collections and state into ``output``."""
    documents, summary = rebuild(tables, providers)
    store = collection_store(output)
    store.records = documents
    store.save()
    current.update(mapping_version=MAPPING_VERSION, dropped=summary["dropped"])
    save_tables(output, tables)
    save_current(output, current)
    return summary


async def discover_provider(provider: dict, *, client=None) -> dict:
    """The provider's single-language discovery scope."""
    connection = client or Client(provider["base_url"])
    try:
        async for scope in discover(provider, client=connection):
            return scope
    finally:
        if client is None:
            await connection.http.aclose()
    raise ValueError("Discovery yielded no scope")


async def fetch_metadata(record: dict, *, client) -> dict:
    """The trimmed structure document of one discovered table."""
    structure = await infer_structure(
        client,
        record["table_url"],
        source_uri=(record.get("catalogue") or {}).get("source_distribution"),
    )
    return trim(table_document(record, structure))


async def run(
    providers: list[dict],
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
    baseline = manifest(data) if data.exists() else {}
    tables, current = load_tables(data), load_current(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    factory = client_factory or default_client
    pacer = Pacer(
        1 / requests_per_second, budget=max_requests, max_throttles=MAX_THROTTLES
    )
    progress = Progress("entrysoft", "entryscape")
    counts = dict.fromkeys(COUNTS, 0)
    errors, deferred, requests = [], [], 0
    interrupted = None
    wanted = set(selected) if selected else None
    chosen = [p for p in providers if wanted is None or p["code"] in wanted]
    for code in sorted((wanted or set()) - {p["code"] for p in providers}):
        errors.append(
            {"stage": "discovery", "resource": code, "error": "Unknown provider"}
        )

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
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
            "record": record,
            "markers": markers(record),
            "harvested_at": stamp,
            "document": None,
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
            entry.update(
                status="ok",
                rowcount=structure["rowcount"],
                strategy=structure["strategy"],
                requests=structure["requests"],
                document=trim(table_document(record, structure)),
            )
            counts["harvested"] += 1
        tables[key] = entry
        return entry["status"]

    try:
        if preflight:
            progress.update(phase="discovery", current="preflight")
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
                    raise Blocked(f"{host} is not reachable; not starting") from error
        table_errors = 0
        progress.update(phase="metadata", total=len(chosen), processed=0)
        for position, provider in enumerate(chosen, start=1):
            code = provider["code"]
            progress.update(current=code, operation="discovery")
            try:
                scope = await call(
                    provider["base_url"],
                    lambda client, provider=provider: discover_provider(
                        provider, client=client
                    ),
                )
            except Blocked:
                raise
            except Exception as error:
                fail("discovery", code, error)
                current["providers"][code] = {"checked_at": stamp, "complete": False}
                continue
            if scope["errors"]:
                for text in scope["errors"]:
                    fail("discovery", code, RuntimeError(text))
                if any("Connect" in e and not unresolvable(e) for e in scope["errors"]):
                    raise Blocked(f"{provider['base_url']}: {scope['errors']}")
            current["providers"][code] = {
                "checked_at": stamp,
                "complete": scope["complete"],
                "tables": len(scope["records"]),
            }
            listed = {table_key(r): r for r in scope["records"]}
            if scope["complete"] and wanted is None:
                for key in [
                    k
                    for k in tables
                    if k.startswith(code + "/")
                    and k not in listed
                    and within_catalog_scope(provider, tables[k].get("record", {}))
                ]:
                    tables.pop(key)
                    counts["removed"] += 1
            for key, record in listed.items():
                previous = tables.get(key)
                settled = previous is not None and previous.get("status") in (
                    "ok",
                    "empty",
                    "deferred",
                )
                if settled and any(markers(record).values()):
                    if previous["markers"] == markers(record):
                        counts["unchanged"] += 1
                        continue
                elif settled:
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
                        continue
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
                        "record": record,
                        "markers": markers(record),
                        "harvested_at": stamp,
                        "status": "error",
                        "error": fail("metadata", key, error),
                        "document": (previous or {}).get("document"),
                    }
                    counts["failed"] += 1
                    table_errors += 1
                    if table_errors >= MAX_TABLE_ERRORS:
                        raise Blocked(
                            f"{table_errors} unexpected table failures"
                        ) from error
            progress.update(processed=position, succeeded=counts["harvested"])
    except Blocked as error:
        interrupted = str(error)
        fail("run", "budget", error)
    progress.update(failed=counts["failed"])

    summary = publish(tables, current, providers, output)
    counts["datasets"], counts["lists"] = summary["datasets"], len(summary["lists"])
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
        "dropped_tables": summary["dropped"],
        "list_datasets": summary["lists"],
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    return result


def collect(data: Path, output: Path) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale EntryScape artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("EntryScape artifact checksum mismatch")
    for relative in result["manifest"]:
        if relative not in {TABLES, CURRENT, REPORT} and not (
            relative.startswith("metadata/") and relative.endswith(".jsonl.gz")
        ):
            raise ValueError(f"Unexpected EntryScape artifact path: {relative}")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    for stale in (
        (data / "metadata").glob("*.jsonl.gz") if (data / "metadata").exists() else []
    ):
        if f"metadata/{stale.name}" not in result["manifest"]:
            stale.unlink()
    return result

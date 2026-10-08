"""Refresh, reconciliation and provider workers; collectors are the only writers."""

import asyncio
import time
from pathlib import Path
from urllib.parse import unquote

from metadata_harvest.adapters.pxweb_v1.discovery import discover_provider
from metadata_harvest.adapters.pxweb_v1.metadata import get_pxweb_metadata
from metadata_harvest.storage import (
    JsonlStore,
    filename,
    group_records,
    identity,
    location,
    now,
    read,
    refresh_identity,
    timestamp,
    validate_dataset,
    write,
)


def prepare(data, providers):
    current = Path(data) / "work/current.json"
    state = read(current) if current.exists() else None
    if state and any(task["status"] == "pending" for task in state["tasks"]):
        codes = sorted(
            {
                task["provider_code"]
                for task in state["tasks"]
                if task["status"] == "pending"
            }
        )
        if not set(codes).issubset(p["code"] for p in providers):
            raise ValueError(
                "Unfinished work contains a removed provider; reconcile its work list explicitly"
            )
        return {
            "run_id": state["run_id"],
            "phase": "metadata",
            "wave": state["wave"],
            "providers": codes,
        }
    run_id = now().replace("-", "").replace(":", "").replace("+0000", "Z")
    return {
        "run_id": run_id,
        "phase": "refresh",
        "wave": 0,
        "providers": [p["code"] for p in providers],
    }


async def refresh_provider(provider, run_id, output):
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
            write(Path(output) / "result.json", result)
    finally:
        result["finished_at"] = now()
        write(Path(output) / "result.json", result)
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
            raise ValueError("Duplicate or stale refresh artifact")
        received[code] = result
    expected_codes = {p["code"] for p in providers}
    if not set(received).issubset(expected_codes):
        raise ValueError("Unexpected provider artifact")
    for provider in providers:
        code = provider["code"]
        rows = received.get(code, {}).get("scopes", [])
        indexed = {(row["language"], row["database_id"]): row for row in rows}
        if len(indexed) != len(rows):
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


def collect_refresh(data, providers, results, run_id):
    data = Path(data)
    scopes, errors = _scopes(providers, results, run_id)
    fresh = []
    for (code, _, _), scope in scopes.items():
        fresh.extend(
            {**row, "provider_code": code, "dataset_code": row["id"]}
            for row in scope["records"]
        )
    grouped = {identity(row): row for row in group_records(fresh)}
    listings = JsonlStore(data / "table-records")
    active = JsonlStore(data / "metadata")
    previous = {identity(row): row for row in listings.records.values()}
    previous_names = set(listings.records)
    known = dict(active.records)
    for name, document in known.items():
        if name not in previous_names:
            saved = document.get("extension", {}).get("metadata_harvest", {})
            if filename(saved) != name:
                raise ValueError(f"Saved metadata identity mismatch: {name}")
            previous.setdefault(identity(saved), saved)
    tasks, verified = [], []
    counts = dict(reused=0, removed=0, deferred=0)

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
                if name in active.records:
                    del active.records[name]
                    counts["removed"] += 1
                listings.records.pop(name, None)
            else:
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
                if timestamp(new["published"]) <= timestamp(old["published"]):
                    new = {**old, "paths": new["paths"]}
                    listings.records[name] = new
                    if name in active.records:
                        completed = active.records[name]
                        if completed.get("updated") == new["published"]:
                            completed = refresh_identity(completed, new)
                            validate_dataset(completed, new)
                            active.records[name] = completed
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
        existing = None
        if name in active.records:
            try:
                existing = active.records[name]
                if identity(existing["extension"]["metadata_harvest"]) != key:
                    raise ValueError("Saved metadata identity mismatch")
                if existing.get("updated") == new["published"]:
                    existing = refresh_identity(existing, new)
                    validate_dataset(existing, new)
                    active.records[name] = existing
                    counts["reused"] += 1
                    continue
            except (ValueError, KeyError, TypeError) as error:
                errors.append(
                    {
                        "provider_code": key[0],
                        "dataset_code": key[1],
                        "language": key[2],
                        "stage": "saved_metadata",
                        "message": str(error),
                    }
                )
        tasks.append(
            {
                "filename": name,
                "provider_code": key[0],
                "status": "pending",
                "kind": "updated" if name in active.records else "created",
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
    refresh_name = "refresh.json"
    listings.save()
    active.save()
    write(data / "reports" / refresh_name, report)
    state = {
        "run_id": run_id,
        "stage": "metadata",
        "wave": 1,
        "started_at": started_at,
        "refresh_report": refresh_name,
        "counts": counts,
        "refresh_errors": errors,
        "tasks": tasks,
        "no_progress_waves": 0,
    }
    write(data / "work/current.json", state)
    return state


async def build_provider(
    data, provider, run_id, wave, output, *, budget_seconds=19_200
):
    data, output = Path(data), Path(output)
    state = read(data / "work/current.json")
    if (state["run_id"], state["wave"]) != (run_id, wave):
        raise ValueError("Stale metadata work list")
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
    completed = JsonlStore(output / "metadata")
    deadline = time.monotonic() + budget_seconds
    for task in state["tasks"]:
        if task["status"] != "pending" or task["provider_code"] != provider["code"]:
            continue
        if time.monotonic() + 900 > deadline:
            break
        outcome = {"filename": task["filename"], "started_at": now()}
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
                dataset = await get_pxweb_metadata(
                    record, provider_code=provider["code"]
                )
            validate_dataset(dataset, record)
            previous = completed.records.get(task["filename"])
            completed.records[task["filename"]] = dataset
            try:
                completed.save()
            except Exception:
                if previous is None:
                    completed.records.pop(task["filename"])
                else:
                    completed.records[task["filename"]] = previous
                raise
            outcome["status"] = "success"
        except Exception as error:
            outcome.update(status="failed", error=f"{type(error).__name__}: {error}")
        outcome["finished_at"] = now()
        result["outcomes"].append(outcome)
        write(output / "result.json", result)
        print(f"{provider['code']} {task['filename']}: {outcome['status']}", flush=True)
        await asyncio.sleep(provider.get("rate_limit", 1))
    result["finished_at"] = now()
    write(output / "result.json", result)
    return result


def collect_metadata(data, result_paths, run_id, wave):
    data = Path(data)
    state = read(data / "work/current.json")
    if (state["run_id"], state["wave"]) != (run_id, wave):
        raise ValueError("Stale metadata collector")
    listings = JsonlStore(data / "table-records")
    active = JsonlStore(data / "metadata")
    tasks = {task["filename"]: task for task in state["tasks"]}
    seen, providers = set(), set()
    progress = 0
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
        completed = JsonlStore(path.parent / "metadata")
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
                dataset = completed.records[name]
                validate_dataset(dataset, record)
                active.records[name] = dataset
            task.update(outcome)
            progress += 1
    pending = [task for task in tasks.values() if task["status"] == "pending"]
    state["no_progress_waves"] = 0 if progress else state["no_progress_waves"] + 1
    state["wave"] += 1
    state["stage"] = "metadata" if pending else "complete"
    if pending and state["no_progress_waves"] >= 3:
        state["stage"] = "interrupted"
    counts = {
        **state["counts"],
        "created": 0,
        "updated": 0,
        "failed": 0,
        "unfinished": len(pending),
    }
    for task in tasks.values():
        if task["status"] == "success":
            counts[task["kind"]] += 1
        elif task["status"] == "failed":
            counts["failed"] += 1
    status = (
        "interrupted"
        if state["stage"] == "interrupted"
        else "running"
        if pending
        else "completed_with_errors"
        if counts["failed"] or state["refresh_errors"]
        else "success"
    )
    report = {
        "run_id": run_id,
        "status": status,
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
        ],
    }
    if not pending:
        report["finished_at"] = report["updated_at"]
    active.save()
    write(data / "reports/harvest.json", report)
    if pending:
        write(data / "work/current.json", state)
    else:
        (data / "work/current.json").unlink(missing_ok=True)
    return {
        "continue": bool(pending) and state["stage"] != "interrupted",
        "status": status,
        "counts": counts,
    }

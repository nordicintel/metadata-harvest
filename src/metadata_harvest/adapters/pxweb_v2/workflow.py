"""SCB worker and validating collector: current state, no metadata archives."""

import hashlib
import shutil
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from metadata_harvest.adapters.pxweb_v2.client import Client
from metadata_harvest.adapters.pxweb_v2.discovery import discover_provider
from metadata_harvest.adapters.pxweb_v2.metadata import fetch_codelist, fetch_metadata
from metadata_harvest.adapters.pxweb_v2.validation import (
    codelist_ids,
    validate_codelist,
    validate_metadata,
)
from metadata_harvest.storage import JsonlStore, filename, now, read, write


def code_identity(document: dict) -> dict:
    return {
        "provider_code": document["provider_code"],
        "language": document["language"],
        "dataset_code": document["codelist"]["id"],
    }


def code_key(code: str, language: str) -> str:
    return filename(
        {"provider_code": "scb", "dataset_code": code, "language": language}
    )


def manifest(root: Path) -> dict:
    """Content fingerprint for optimistic concurrency and artifact integrity."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "result.json":
            with path.open("rb") as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def load_stores(root: Path) -> tuple[JsonlStore, JsonlStore, JsonlStore]:
    return (
        JsonlStore(root / "metadata"),
        JsonlStore(root / "codelists", identify=code_identity),
        JsonlStore(root / "table-records"),
    )


def validate_provider(provider: dict) -> None:
    if (
        provider["code"] != "scb"
        or set(provider["languages"]) != {"sv", "en"}
        or len(provider["languages"]) != 2
    ):
        raise ValueError("The v2 job currently supports only SCB sv/en")


def _prune_codes(metadata: JsonlStore, codes: JsonlStore) -> None:
    required = {
        code_key(code, doc["extension"]["metadata_harvest"]["language"])
        for doc in metadata.records.values()
        for code in codelist_ids(doc)
    }
    codes.records = {k: v for k, v in codes.records.items() if k in required}


def _snapshot(output, metadata, codes, listings, state, base, *, final=False):
    """Batch all current documents; commit the manifest last to certify a snapshot."""
    marker = output / "result.json"
    previous_buffer = read(marker)["snapshot"] if marker.exists() else None
    buffer = "buffer-1" if previous_buffer == "buffer-0" else "buffer-0"
    destination = output / buffer
    if destination.exists():
        shutil.rmtree(destination)
    for name, doc in metadata.records.items():
        language = doc["extension"]["metadata_harvest"]["language"]
        if any(
            code_key(code, language) in state["removed_codelists"]
            for code in codelist_ids(doc)
        ):
            if name not in state["tasks"]:
                state["counts"]["reused"] -= 1
                state["tasks"][name] = {"kind": "updated"}
            state["tasks"][name].update(
                status="failed",
                error="A referenced codelist is confirmed unavailable at the source",
            )
    _prune_codes(metadata, codes)
    for store in (metadata, codes, listings):
        store.directory = destination / store.directory.name
        store.save()
    pending = sum(task["status"] == "pending" for task in state["tasks"].values())
    counts = {
        **state["counts"],
        "created": 0,
        "updated": 0,
        "failed": 0,
        "unfinished": pending,
    }
    errors = list(state["discovery_errors"])
    for name, task in state["tasks"].items():
        if task["status"] == "success":
            counts[task["kind"]] += 1
        elif task["status"] == "failed":
            counts["failed"] += 1
            errors.append(
                {"filename": name, "stage": "metadata", "error": task["error"]}
            )
    interrupted = pending and state.get("no_progress_waves", 0) >= 3
    status = (
        "interrupted"
        if interrupted
        else "running"
        if pending
        else "completed_with_errors"
        if errors
        else "success"
    )
    report = {
        "run_id": state["run_id"],
        "wave": state["wave"],
        "full": state["full"],
        "started_at": state["started_at"],
        "updated_at": now(),
        "status": status,
        "counts": counts,
        "errors": errors,
        "scopes": state["scopes"],
        "removed_tables": state["removed_tables"],
        "removed_codelists": state["removed_codelists"],
    }
    write(destination / "reports/harvest.json", report)
    write(
        destination / "reports/refresh.json",
        {
            "run_id": state["run_id"],
            "scopes": state["scopes"],
            "errors": state["discovery_errors"],
        },
    )
    if pending:
        write(destination / "work/current.json", {**state, "wave": state["wave"] + 1})
    result = {
        **report,
        "continue": bool(pending) and not interrupted,
        "base": base,
        "manifest": manifest(destination),
        "snapshot": buffer,
        "provider": state["provider"],
        "final": final,
    }
    write(output / "result.json", result)
    # Only temporary transport buffers exist; retain one certified candidate.
    if previous_buffer is not None:
        shutil.rmtree(output / previous_buffer)
    return result


async def run(
    provider: dict,
    data: str | Path,
    output: str | Path,
    *,
    full: bool = False,
    budget_seconds: float = 19_200,
    client: Client | None = None,
) -> dict:
    """Build a complete candidate artifact; only collect() changes published data."""
    validate_provider(provider)
    data, output = Path(data), Path(output)
    if (
        data.resolve() == output.resolve()
        or data.resolve() in output.resolve().parents
        or output.resolve() in data.resolve().parents
    ):
        raise ValueError("Worker output must be separate from published data")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Worker output must be empty")
    if client is None:
        async with Client(provider["base_api_url"]) as owned:
            return await run(
                provider,
                data,
                output,
                full=full,
                budget_seconds=budget_seconds,
                client=owned,
            )
    deadline = time.monotonic() + budget_seconds
    base = manifest(data)
    metadata, codes, listings = load_stores(data)
    # Source stores are read-only to the worker, including legacy migration inputs.
    for store in (metadata, codes, listings):
        store.directory = output / store.directory.name
        store._legacy.clear()
    checkpoint = data / "work/current.json"
    if checkpoint.exists():
        state = read(checkpoint)
        if state["provider"] != provider:
            raise ValueError(
                "Unfinished work belongs to another provider configuration"
            )
        client.configure(await client.get("config"))
    else:
        previous_report = data / "reports/harvest.json"
        failed = (
            {e.get("filename") for e in read(previous_report)["errors"]}
            if previous_report.exists()
            else set()
        )
        state = {
            "run_id": uuid4().hex,
            "wave": 1,
            "full": full,
            "provider": provider,
            "started_at": now(),
            "tasks": {},
            "scopes": [],
            "discovery_errors": [],
            "refreshed_codes": [],
            "code_errors": {},
            "removed_tables": [],
            "removed_codelists": [],
            "no_progress_waves": 0,
            "counts": {
                "reused": 0,
                "removed": 0,
                "discovered": 0,
                "codelists_fetched": 0,
            },
        }
        async for scope in discover_provider(provider, client=client):
            language = scope["language"]
            state["scopes"].append(
                {
                    "language": language,
                    "complete": scope["complete"],
                    "count": len(scope["records"]),
                    "observed_ids": scope["observed_ids"],
                }
            )
            state["discovery_errors"].extend(
                {"language": language, "stage": "discovery", "error": error}
                for error in scope["errors"]
            )
            current = {filename(row): row for row in scope["records"]}
            state["counts"]["discovered"] += len(current)
            if scope["complete"]:
                for name in set(metadata.records) | set(listings.records):
                    previous = (
                        listings.records.get(name)
                        or metadata.records[name]["extension"]["metadata_harvest"]
                    )
                    if previous["language"] == language and name not in current:
                        if metadata.records.pop(name, None) is not None:
                            state["counts"]["removed"] += 1
                        listings.records.pop(name, None)
            for name, row in current.items():
                listings.records[name] = row
                saved = metadata.records.get(name)
                reusable = False
                if saved is not None and not full and name not in failed:
                    try:
                        validate_metadata(saved, row)
                        reusable = saved["extension"]["metadata_harvest"] == row
                        for code in codelist_ids(saved):
                            validate_codelist(
                                codes.records[code_key(code, language)]["codelist"],
                                code,
                                language,
                            )
                    except (ValueError, KeyError, TypeError):
                        reusable = False
                if reusable:
                    state["counts"]["reused"] += 1
                else:
                    state["tasks"][name] = {
                        "status": "pending",
                        "kind": "updated" if saved is not None else "created",
                    }
    completed = 0
    last_save = time.monotonic()
    for name, task in list(state["tasks"].items()):
        if task["status"] != "pending":
            continue
        if time.monotonic() + 180 > deadline:
            break
        row = listings.records[name]
        language = row["language"]
        try:
            document = await fetch_metadata(row, client=client)
            for code in sorted(codelist_ids(document)):
                key = code_key(code, language)
                if key in state["code_errors"]:
                    raise ValueError(state["code_errors"][key])
                if key not in state["refreshed_codes"] or key not in codes.records:
                    try:
                        payload = await fetch_codelist(code, language, client=client)
                    except Exception as error:
                        if isinstance(
                            error, httpx.HTTPStatusError
                        ) and error.response.status_code in {404, 410}:
                            try:
                                # Two source responses confirm absence; a transient
                                # second failure does not justify deletion.
                                await fetch_codelist(code, language, client=client)
                            except httpx.HTTPStatusError as confirmation:
                                if confirmation.response.status_code in {404, 410}:
                                    codes.records.pop(key, None)
                                    state["removed_codelists"].append(key)
                            except Exception:
                                pass
                        state["code_errors"][key] = (
                            f"Codelist {code}/{language}: {error}"
                        )
                        raise
                    codes.records[key] = {
                        "provider_code": "scb",
                        "language": language,
                        "codelist": payload,
                    }
                    state["refreshed_codes"].append(key)
                    state["counts"]["codelists_fetched"] += 1
            metadata.records[name] = document
            listings.records[name] = deepcopy(document["extension"]["metadata_harvest"])
            task["status"] = "success"
        except Exception as error:
            # A 404 during a catalogue race is confirmed against the table endpoint.
            if (
                isinstance(error, httpx.HTTPStatusError)
                and error.response.status_code in {404, 410}
                and "/tables/" in str(error.request.url)
            ):
                try:
                    await client.get(
                        f"tables/{quote(row['dataset_code'], safe='')}", lang=language
                    )
                except httpx.HTTPStatusError as confirmation:
                    if confirmation.response.status_code in {404, 410}:
                        if metadata.records.pop(name, None) is not None:
                            state["counts"]["removed"] += 1
                        listings.records.pop(name, None)
                        state["removed_tables"].append(name)
                except Exception:
                    pass
            task.update(status="failed", error=f"{type(error).__name__}: {error}")
        completed += 1
        if completed % 100 == 0 or time.monotonic() - last_save >= 60:
            _snapshot(output, metadata, codes, listings, state, base)
            last_save = time.monotonic()
            print(f"SCB wave {state['wave']}: {completed} tables processed", flush=True)
    state["no_progress_waves"] = (
        0 if completed else state.get("no_progress_waves", 0) + 1
    )
    return _snapshot(output, metadata, codes, listings, state, base, final=True)


def _validate_reconciliation(result, output, metadata, codes, listings, previous):
    """Independently check removal evidence and status before publishing any file."""
    report = read(output / "reports/harvest.json")
    if any(result.get(k) != value for k, value in report.items()):
        raise ValueError("Harvest report differs from worker result")
    refresh = read(output / "reports/refresh.json")
    if refresh["run_id"] != result["run_id"] or refresh["scopes"] != result["scopes"]:
        raise ValueError("Refresh report identity or scopes differ")
    scopes = {s["language"]: s for s in result["scopes"]}
    if set(scopes) != {"sv", "en"} or len(result["scopes"]) != 2:
        raise ValueError("Missing or duplicate discovery scope")
    failed = {e.get("filename") for e in result["errors"] if e.get("filename")}
    pending = set()
    checkpoint = output / "work/current.json"
    if checkpoint.exists():
        state = read(checkpoint)
        if (
            state["run_id"] != result["run_id"]
            or state["wave"] != result["wave"] + 1
            or state["provider"] != result["provider"]
            or state["full"] != result["full"]
        ):
            raise ValueError("Invalid continuation checkpoint identity")
        pending = {
            name for name, task in state["tasks"].items() if task["status"] == "pending"
        }
        if any(
            task["status"] not in {"pending", "success", "failed"}
            for task in state["tasks"].values()
        ):
            raise ValueError("Invalid continuation task status")
    if (
        len(pending) != result["counts"]["unfinished"]
        or len(failed) != result["counts"]["failed"]
        or bool(pending) != checkpoint.exists()
    ):
        raise ValueError("Report counts do not match pending or failed work")
    expected_status = (
        "interrupted"
        if pending and not result["continue"]
        else "running"
        if pending
        else "completed_with_errors"
        if result["errors"]
        else "success"
    )
    if result["status"] != expected_status or (not pending and result["continue"]):
        raise ValueError("Inconsistent harvest status")
    confirmed = set(result["removed_tables"])
    if not confirmed.issubset(failed):
        raise ValueError("Confirmed removals lack source-error evidence")
    for name in set(previous.records) - set(metadata.records):
        saved = previous.records[name]["extension"]["metadata_harvest"]
        scope = scopes[saved["language"]]
        if name not in confirmed and (
            not scope["complete"]
            or saved["dataset_code"] in scope["observed_ids"]
            or name in listings.records
        ):
            raise ValueError("Metadata removal is not justified by complete discovery")
    for name, row in listings.records.items():
        if row["provider_code"] != "scb" or row["language"] not in scopes:
            raise ValueError("Unexpected listing identity")
        if name not in metadata.records and name not in failed | pending:
            raise ValueError(
                "Listing has neither accepted metadata nor a reported failure"
            )
    for language, scope in scopes.items():
        if scope["complete"]:
            observed = set(scope["observed_ids"])
            if len(observed) != scope["count"]:
                raise ValueError(
                    "Discovery scope count differs from observed identities"
                )
            expected = {
                filename(
                    {"provider_code": "scb", "dataset_code": code, "language": language}
                )
                for code in observed
            } - confirmed
            actual = {
                name
                for name, row in listings.records.items()
                if row["language"] == language
            }
            if expected != actual:
                raise ValueError("Complete discovery scope does not match listings")
    referenced = {
        code_key(code, doc["extension"]["metadata_harvest"]["language"])
        for doc in metadata.records.values()
        for code in codelist_ids(doc)
    }
    if set(codes.records) - referenced or referenced - set(codes.records) - set(
        result["removed_codelists"]
    ):
        raise ValueError("Unresolved or unreferenced codelists in candidate")


def collect(data: str | Path, output: str | Path, run_id: str, wave: int) -> dict:
    """Validate the entire artifact before applying it; Git publishes all files together."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if (
        result["run_id"] != run_id
        or result["wave"] != wave
        or manifest(data) != result["base"]
    ):
        raise ValueError("Stale worker artifact or changed published state")
    if result.get("snapshot") not in {"buffer-0", "buffer-1"}:
        raise ValueError("Invalid candidate buffer")
    output = output / result["snapshot"]
    if manifest(output) != result["manifest"]:
        raise ValueError("Worker artifact content does not match its manifest")
    validate_provider(result["provider"])
    for relative in result["manifest"]:
        path = Path(relative)
        allowed = (
            len(path.parts) == 2
            and path.parts[0] in {"metadata", "codelists", "table-records"}
            and path.suffix == ".jsonl"
        ) or relative in {
            "reports/refresh.json",
            "reports/harvest.json",
            "work/current.json",
        }
        if not allowed:
            raise ValueError(f"Unexpected artifact path: {relative}")
        if (output / relative).stat().st_size >= 100 * 1024 * 1024:
            raise ValueError(
                f"Collection exceeds GitHub's 100 MiB limit: {relative}; no content was omitted or split"
            )
    metadata, codes, listings = load_stores(output)
    failed = {e.get("filename") for e in result["errors"]}
    previous = JsonlStore(data / "metadata")
    _validate_reconciliation(result, output, metadata, codes, listings, previous)
    for name, doc in metadata.records.items():
        saved = doc["extension"]["metadata_harvest"]
        if saved["provider_code"] != "scb" or saved["language"] not in {"sv", "en"}:
            raise ValueError("Unexpected metadata identity")
        if name in failed and previous.records.get(name) == doc:
            continue
        validate_metadata(doc, saved)
        if name not in listings.records or listings.records[name] != saved:
            # A failed replacement may retain the old document beside a new listing.
            if previous.records.get(name) != doc:
                raise ValueError("Metadata does not match accepted listing")
        for code in codelist_ids(doc):
            validate_codelist(
                codes.records[code_key(code, saved["language"])]["codelist"],
                code,
                saved["language"],
            )
    for document in codes.records.values():
        if document["provider_code"] != "scb" or document["language"] not in {
            "sv",
            "en",
        }:
            raise ValueError("Unexpected codelist identity")
        validate_codelist(
            document["codelist"], document["codelist"]["id"], document["language"]
        )
    for incoming in (metadata, codes, listings):
        target = JsonlStore(data / incoming.directory.name, identify=incoming.identify)
        target.records = incoming.records
        target.save()
    for relative in (
        "reports/refresh.json",
        "reports/harvest.json",
        "work/current.json",
    ):
        if (output / relative).exists():
            write(data / relative, read(output / relative))
        else:
            (data / relative).unlink(missing_ok=True)
    if manifest(data) != result["manifest"]:
        raise ValueError(
            "Collected state differs from validated artifact; do not publish"
        )
    return result

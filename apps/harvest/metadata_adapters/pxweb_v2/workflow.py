"""SCB worker and validating collector: current state, no metadata archives.

A run lists both language catalogues, fetches metadata only for tables whose
catalogue entry changed (or everything in full mode), fetches referenced codelists
once, and writes the candidate exactly once at the end. Work left over when the
time budget runs out is reported as failed and picked up by the next run.
"""

import logging
import shutil
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from apps.harvest.artifacts import report_status
from apps.harvest.metadata_adapters.pxweb_v1.mapping import source_record
from apps.harvest.metadata_adapters.pxweb_v2.client import Client
from apps.harvest.metadata_adapters.pxweb_v2.discovery import discover_provider
from apps.harvest.metadata_adapters.pxweb_v2.metadata import (
    fetch_codelist,
    fetch_metadata,
)
from apps.harvest.metadata_adapters.pxweb_v2.validation import (
    codelist_ids,
    validate_codelist,
    validate_metadata,
)
from apps.harvest.storage import JsonlStore, filename, now, read, write
from apps.operations.progress import Progress
from apps.publication.artifacts import manifest

logger = logging.getLogger("pxweb_v2_logger")

REMOVED_CODELIST = "Codelist confirmed removed at the source (HTTP 404)"


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


def load_stores(root: Path) -> tuple[JsonlStore, JsonlStore, JsonlStore]:
    return (
        JsonlStore(root / "metadata", compressed=True),
        JsonlStore(root / "codelists", identify=code_identity, compressed=True),
        JsonlStore(root / "table-records"),
    )


def validate_provider(provider: dict) -> None:
    if (
        provider["code"] != "scb"
        or set(provider["languages"]) != {"sv", "en"}
        or len(provider["languages"]) != 2
    ):
        raise ValueError("The v2 job currently supports only SCB sv/en")


def _referenced(metadata: JsonlStore) -> set[str]:
    return {
        code_key(code, source_record(doc)["language"])
        for doc in metadata.records.values()
        for code in codelist_ids(doc)
    }


def _write_candidate(output, metadata, codes, listings, state, base):
    """Write every store once, then the reports; the manifest certifies the set."""
    required = _referenced(metadata)
    codes.records = {k: v for k, v in codes.records.items() if k in required}
    for store in (metadata, codes, listings):
        store.directory = output / store.directory.name
        store.save()
    counts = {**state["counts"], "created": 0, "updated": 0, "failed": 0}
    errors = list(state["discovery_errors"])
    code_errors = {
        key: error for key, error in state["code_errors"].items() if key in required
    }
    for key in required - set(codes.records):
        code_errors.setdefault(
            key, "Referenced codelist is unavailable; retrieval has not completed"
        )
    counts["codelists_failed"] = len(code_errors)
    errors.extend(
        {"codelist_key": key, "stage": "codelist", "error": error}
        for key, error in sorted(code_errors.items())
    )
    for name, task in state["tasks"].items():
        if task["status"] == "success":
            counts[task["kind"]] += 1
        else:
            counts["failed"] += 1
            errors.append(
                {"filename": name, "stage": "metadata", "error": task["error"]}
            )
    report = {
        "run_id": state["run_id"],
        "full": state["full"],
        "started_at": state["started_at"],
        "updated_at": now(),
        "status": report_status(pending=0, errors=errors),
        "counts": counts,
        "errors": errors,
        "scopes": state["scopes"],
        "removed_tables": state["removed_tables"],
        "removed_codelists": sorted(state["removed_codelists"]),
    }
    write(output / "reports/harvest.json", report)
    write(
        output / "reports/refresh.json",
        {
            "run_id": state["run_id"],
            "scopes": state["scopes"],
            "errors": state["discovery_errors"],
        },
    )
    result = {
        **report,
        "base": base,
        "manifest": manifest(output),
        "provider": state["provider"],
    }
    write(output / "result.json", result)
    logger.info(
        "Candidate written: status=%s, errors=%d, counts=%s",
        result["status"],
        len(errors),
        counts,
    )
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
    logger.info(
        "PXWeb v2 run starting: provider=%s languages=%s mode=%s budget=%.0fs",
        provider["code"],
        provider["languages"],
        "full" if full else "refresh",
        budget_seconds,
    )
    deadline = time.monotonic() + budget_seconds
    progress = Progress("pxweb_v2", provider["code"])
    progress.update(phase="loading", full=full)
    base = manifest(data)
    metadata, codes, listings = load_stores(data)
    logger.info(
        "Published state loaded: %d metadata documents, %d codelists, %d table records",
        len(metadata.records),
        len(codes.records),
        len(listings.records),
    )
    for store in (metadata, codes, listings):
        store._legacy.clear()
    previous_report = data / "reports/harvest.json"
    report = read(previous_report) if previous_report.exists() else {}
    failed = {e.get("filename") for e in report.get("errors", []) if e.get("filename")}
    # Codelists confirmed 404 are reported, not re-requested on every refresh.
    dead_codes = set(report.get("removed_codelists", []))
    failed_codes = {
        e.get("codelist_key")
        for e in report.get("errors", [])
        if e.get("codelist_key") and e["codelist_key"] not in dead_codes
    }
    state = {
        "run_id": uuid4().hex,
        "full": full,
        "provider": provider,
        "started_at": now(),
        "tasks": {},
        "scopes": [],
        "discovery_errors": [],
        "refreshed_codes": set(),
        "code_errors": {},
        "removed_tables": [],
        "removed_codelists": set() if full else set(dead_codes),
        "counts": {
            "reused": 0,
            "removed": 0,
            "discovered": 0,
            "codelists_fetched": 0,
        },
    }
    logger.info(
        "Run %s (%s); previous report lists %d failed tables, %d failed codelists "
        "to retry and %d codelists confirmed removed",
        state["run_id"],
        "full run, every table is re-fetched" if full else "refresh run",
        len(failed),
        len(failed_codes),
        len(dead_codes),
    )
    progress.update(phase="discovery")
    async for scope in discover_provider(provider, client=client):
        language = scope["language"]
        progress.update(
            language=language,
            discovered=state["counts"]["discovered"] + len(scope["records"]),
        )
        for error in scope["errors"]:
            progress.error(
                "discovery",
                "tables",
                language,
                error,
                url=provider["base_api_url"] + "/tables",
            )
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
        removed_before = state["counts"]["removed"]
        selection = Counter()
        if scope["complete"]:
            for name in set(metadata.records) | set(listings.records):
                previous = listings.records.get(name) or source_record(
                    metadata.records[name]
                )
                if previous["language"] == language and name not in current:
                    if metadata.records.pop(name, None) is not None:
                        state["counts"]["removed"] += 1
                    listings.records.pop(name, None)
            logger.info(
                "Scope %s is complete: %d stored tables absent from the catalogue "
                "were removed",
                language,
                state["counts"]["removed"] - removed_before,
            )
        else:
            logger.warning(
                "Scope %s is incomplete: stored tables missing from the partial "
                "catalogue are kept, not treated as removed",
                language,
            )
        for name, row in current.items():
            listings.records[name] = row
            saved = metadata.records.get(name)
            reusable = False
            needs_codes = False
            reason = (
                "full run"
                if full
                else "new table"
                if saved is None
                else "previously failed"
                if name in failed
                else None
            )
            if saved is not None and not full and name not in failed:
                try:
                    validate_metadata(saved, row)
                    reusable = source_record(saved) == row
                    if not reusable:
                        reason = "catalogue entry changed"
                except (ValueError, KeyError, TypeError) as error:
                    reason = "saved metadata invalid"
                    logger.debug(
                        "Saved metadata for %s failed validation: %s: %s",
                        name,
                        type(error).__name__,
                        error,
                    )
            if reusable:
                for code in codelist_ids(saved):
                    key = code_key(code, language)
                    if key in dead_codes:
                        continue
                    try:
                        validate_codelist(
                            codes.records[key]["codelist"], code, language
                        )
                        needs_codes |= key in failed_codes
                    except (ValueError, KeyError, TypeError):
                        needs_codes = True
                if needs_codes:
                    reason = "codelist missing, invalid or previously failed"
            if reusable and not needs_codes:
                state["counts"]["reused"] += 1
                selection["reused unchanged"] += 1
            else:
                state["tasks"][name] = {
                    "status": "pending",
                    "kind": "reused"
                    if reusable
                    else "updated"
                    if saved is not None
                    else "created",
                }
                selection[reason] += 1
        queued = {k: v for k, v in selection.items() if k != "reused unchanged"}
        logger.info(
            "Scope %s selection: %d catalogue tables -> %d reused unchanged, "
            "%d queued (%s)",
            language,
            len(current),
            selection["reused unchanged"],
            sum(queued.values()),
            ", ".join(f"{k}: {v}" for k, v in sorted(queued.items())) or "none",
        )
    kinds = Counter(task["kind"] for task in state["tasks"].values())
    logger.info(
        "Work plan: %d tasks (%d new, %d updated, %d codelist-only), %d discovered, "
        "%d reused, %d removed",
        sum(kinds.values()),
        kinds["created"],
        kinds["updated"],
        kinds["reused"],
        state["counts"]["discovered"],
        state["counts"]["reused"],
        state["counts"]["removed"],
    )
    completed = succeeded = failed_count = 0
    progress.update(
        phase="metadata",
        run_id=state["run_id"],
        total=len(state["tasks"]),
        reused=state["counts"]["reused"],
        request_interval_seconds=client.interval,
    )
    for name, task in state["tasks"].items():
        if time.monotonic() > deadline:
            task.update(
                status="failed",
                error="Time budget exhausted before this table was fetched",
            )
            failed_count += 1
            continue
        row = listings.records[name]
        language = row["language"]
        progress.update(
            current=row["dataset_code"], language=language, operation="metadata"
        )
        try:
            document = (
                metadata.records[name]
                if task["kind"] == "reused"
                else await fetch_metadata(row, client=client)
            )
            metadata.records[name] = document
            listings.records[name] = deepcopy(source_record(document))
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
                        logger.warning(
                            "Table %s/%s confirmed removed at the source (HTTP %d)",
                            language,
                            row["dataset_code"],
                            confirmation.response.status_code,
                        )
                        if metadata.records.pop(name, None) is not None:
                            state["counts"]["removed"] += 1
                        listings.records.pop(name, None)
                        state["removed_tables"].append(name)
                except Exception as confirm_error:
                    logger.info(
                        "Table %s/%s absence check inconclusive (%s); keeping it",
                        language,
                        row["dataset_code"],
                        type(confirm_error).__name__,
                    )
            task.update(status="failed", error=f"{type(error).__name__}: {error}")
            logger.error(
                "Metadata for %s/%s failed: %s",
                language,
                row["dataset_code"],
                task["error"],
            )
            progress.error(
                "metadata",
                row["dataset_code"],
                language,
                task["error"],
                url=f"{provider['base_api_url']}/tables/{quote(row['dataset_code'], safe='')}/metadata",
            )
        succeeded += task["status"] == "success"
        failed_count += task["status"] == "failed"
        progress.update(succeeded=succeeded, failed=failed_count)
        document = metadata.records.get(name)
        if document is not None:
            for code in sorted(codelist_ids(document)):
                key = code_key(code, language)
                if key in state["code_errors"] or key in state["refreshed_codes"]:
                    continue
                if key in state["removed_codelists"]:
                    state["code_errors"][key] = REMOVED_CODELIST
                    continue
                if task["kind"] == "reused" and key not in failed_codes:
                    try:
                        validate_codelist(
                            codes.records[key]["codelist"], code, language
                        )
                    except (KeyError, ValueError, TypeError):
                        pass
                    else:
                        continue
                progress.update(
                    current=code,
                    language=language,
                    operation="codelist",
                    parent_table=row["dataset_code"],
                )
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
                                logger.warning(
                                    "Codelist %s (%s) confirmed removed at the "
                                    "source (HTTP %d)",
                                    code,
                                    language,
                                    confirmation.response.status_code,
                                )
                                codes.records.pop(key, None)
                                state["removed_codelists"].add(key)
                        except Exception as confirm_error:
                            logger.info(
                                "Codelist %s (%s) absence check inconclusive "
                                "(%s); keeping it",
                                code,
                                language,
                                type(confirm_error).__name__,
                            )
                    state["code_errors"][key] = f"Codelist {code}/{language}: {error}"
                    logger.error(
                        "Codelist %s (%s) for table %s failed: %s: %s",
                        code,
                        language,
                        row["dataset_code"],
                        type(error).__name__,
                        error,
                    )
                    progress.error(
                        "codelist",
                        code,
                        language,
                        f"{type(error).__name__}: {error}",
                        url=f"{provider['base_api_url']}/codelists/{quote(code, safe='')}",
                        table=row["dataset_code"],
                    )
                    continue
                codes.records[key] = {
                    "provider_code": "scb",
                    "language": language,
                    "codelist": payload,
                }
                state["refreshed_codes"].add(key)
                state["counts"]["codelists_fetched"] += 1
                progress.update(
                    codelists_succeeded=state["counts"]["codelists_fetched"]
                )
        completed += 1
        progress.update(processed=completed)
        if completed % 500 == 0:
            logger.info(
                "Progress: %d tables processed (%d succeeded, %d failed)",
                completed,
                succeeded,
                failed_count,
            )
    logger.info(
        "Metadata phase finished: %d tasks processed (%d succeeded, %d failed), "
        "%d codelists fetched",
        completed,
        succeeded,
        failed_count,
        state["counts"]["codelists_fetched"],
    )
    progress.update(phase="writing", current=None)
    result = _write_candidate(output, metadata, codes, listings, state, base)
    progress.update(
        phase="finished", result_status=result["status"], final_counts=result["counts"]
    )
    return result


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
    if len(failed) != result["counts"]["failed"]:
        raise ValueError("Report counts do not match failed work")
    if result["status"] != report_status(pending=0, errors=result["errors"]):
        raise ValueError("Inconsistent harvest status")
    confirmed = set(result["removed_tables"])
    if not confirmed.issubset(failed):
        raise ValueError("Confirmed removals lack source-error evidence")
    for name in set(previous.records) - set(metadata.records):
        saved = source_record(previous.records[name])
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
        if name not in metadata.records and name not in failed:
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
    referenced = _referenced(metadata)
    code_errors = {
        e["codelist_key"] for e in result["errors"] if e["stage"] == "codelist"
    }
    if (
        len(code_errors) != result["counts"]["codelists_failed"]
        or code_errors - referenced
    ):
        raise ValueError("Codelist errors do not match referenced identities")
    if set(codes.records) - referenced or referenced - set(codes.records) - code_errors:
        raise ValueError("Unresolved or unreferenced codelists in candidate")


def collect(data: str | Path, output: str | Path, run_id: str) -> dict:
    """Validate the entire artifact before applying it; Git publishes all files together."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if result["run_id"] != run_id or manifest(data) != result["base"]:
        raise ValueError("Stale worker artifact or changed published state")
    if manifest(output) != result["manifest"]:
        raise ValueError("Worker artifact content does not match its manifest")
    validate_provider(result["provider"])
    for relative in result["manifest"]:
        path = Path(relative)
        allowed = (
            len(path.parts) == 2
            and path.parts[0] in {"metadata", "codelists", "table-records"}
            and (
                path.name.endswith(".jsonl.gz")
                if path.parts[0] != "table-records"
                else path.suffix == ".jsonl"
            )
        ) or relative in {"reports/refresh.json", "reports/harvest.json"}
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
        saved = source_record(doc)
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
            key = code_key(code, saved["language"])
            if key in codes.records:
                validate_codelist(
                    codes.records[key]["codelist"], code, saved["language"]
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
    logger.info(
        "Applying candidate: metadata +%d new, %d changed, -%d removed",
        len(set(metadata.records) - set(previous.records)),
        sum(
            1
            for name, doc in metadata.records.items()
            if name in previous.records and previous.records[name] != doc
        ),
        len(set(previous.records) - set(metadata.records)),
    )
    for incoming in (metadata, codes, listings):
        target = JsonlStore(
            data / incoming.directory.name,
            identify=incoming.identify,
            compressed=incoming.compressed,
        )
        target.records = incoming.records
        target.save()
    for relative in ("reports/refresh.json", "reports/harvest.json"):
        write(data / relative, read(output / relative))
    (data / "work/current.json").unlink(missing_ok=True)
    shutil.rmtree(data / "work", ignore_errors=True)
    if manifest(data) != result["manifest"]:
        raise ValueError(
            "Collected state differs from validated artifact; do not publish"
        )
    return result

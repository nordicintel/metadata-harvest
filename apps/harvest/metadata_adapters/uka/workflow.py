"""Weekly UKÄ refresh with independent indicator failures.

Every run reads the catalogue, each indicator's totals and every period's export
link. An indicator whose links, source record and mapping version are unchanged
keeps its stored source entry after a HEAD check of its newest export; any change
downloads all its exports transiently and stores the catalogue row, totals and
links as responses plus the scan summary the mapping consumes as derived data.
Exports and observations are never stored; an export that cannot be scanned is a
metadata failure that keeps the stored entry.

An indicator is published only while its value endpoint works in the current run
and its entry was verified within ``max_age_days``; otherwise it is withheld in
state while its source files remain. Only a complete, unbounded catalogue proves
an indicator's removal. Documents are built later by ``parse``.
"""

import asyncio
import hashlib
import json
import logging
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.uka.client import UkaClient, Unreachable
from apps.harvest.metadata_adapters.uka.mapping import (
    MAPPING_VERSION,
    Scan,
    source_record,
)
from apps.harvest.metadata_adapters.uka.parse import document_from, stored_key
from apps.harvest.metadata_adapters.uka.source import (
    Entry,
    Indicator,
    parse_catalogue,
    parse_totals,
)
from apps.harvest.metadata_adapters.uka.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    load_state,
    save,
)
from apps.harvest.source_store import SourceStore, harvest_manifest
from apps.harvest.storage import read, write
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest

logger = logging.getLogger("uka_logger")

WINDOW = 4
"""Most export files of one indicator downloaded at once."""
COUNTS = ("rebuilt", "reused", "failed", "withheld", "restored", "removed")


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


async def _all(awaitables):
    """Gather, cancelling the remaining requests when one fails."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        raise


def _allowed_path(relative: str) -> bool:
    return relative in {STATE, REPORT} or relative.startswith("source/")


def catalogue_entries(
    body, errors: list, rows: dict | None = None
) -> tuple[list[Entry], bool]:
    """Parse rows independently; malformed or duplicate rows make it incomplete.

    ``rows`` receives each usable indicator's raw catalogue row and group.
    """
    if not isinstance(body, dict):
        raise ValueError(f"unexpected UKÄ catalogue {str(body)[:200]!r}")
    complete, parsed, raw = True, {}, {}
    for group, items in body.items():
        if not isinstance(items, list):
            logger.warning("Catalogue group %r is not a list; skipping it", group)
            errors.append((group, ValueError("Catalogue group is not a list")))
            complete = False
            continue
        logger.debug("Catalogue group %r lists %d rows", group, len(items))
        for item in items:
            try:
                (entry,) = parse_catalogue({group: [item]})
            except Exception as error:
                logger.warning(
                    "Catalogue row in group %r is malformed and skipped: %s: %s",
                    group,
                    type(error).__name__,
                    error,
                )
                errors.append((str(item)[:200], error))
                complete = False
                continue
            parsed.setdefault(entry.id, []).append(entry)
            raw[entry.id] = {"group": group, "row": item}
    entries = []
    for identifier, found in parsed.items():
        if len(found) > 1:
            logger.warning(
                "Indicator %s is listed %d times in the catalogue; skipping it",
                identifier,
                len(found),
            )
            errors.append((str(identifier), ValueError("Listed more than once")))
            complete = False
        else:
            entries.append(found[0])
            if rows is not None:
                rows[found[0].id] = raw[found[0].id]
    logger.info(
        "Catalogue parsed: %d usable indicators in %d groups (%s)",
        len(entries),
        len(body),
        "complete" if complete else "INCOMPLETE: removals will not be applied",
    )
    return entries, complete


async def export_links(client, indicator: Indicator) -> dict[str, str | None]:
    """Each period's export file URL in UKÄ's period order (None: no export)."""
    links = await _all(client.export_link(indicator.id, p) for p in indicator.periods)
    files = dict(zip(indicator.periods, links, strict=True))
    missing = [period for period, link in files.items() if link is None]
    logger.debug(
        "Indicator %s: %d periods, %d export links, no export for %s",
        indicator.id,
        len(files),
        len(files) - len(missing),
        missing or "none",
    )
    return files


async def reachable(client, files: dict[str, str | None]) -> None:
    """Raise unless the newest existing export file answers HEAD."""
    for period in reversed(list(files)):
        if not files[period]:
            continue
        if await client.available(files[period]):
            logger.debug("HEAD check passed on the export of period %s", period)
            return
        logger.warning("HEAD check failed on the export of period %s", period)
    raise Unreachable("No export file of the indicator is available")


async def scan_exports(client, indicator_id: int, files: dict[str, str | None]):
    """Read every period's export, newest first; returns the scan and skips."""
    scan, skipped = Scan(), {}
    ordered = list(reversed(list(files)))
    logger.debug(
        "Indicator %s: downloading %d periods newest first, %d at a time",
        indicator_id,
        len(ordered),
        WINDOW,
    )
    for start in range(0, len(ordered), WINDOW):
        window = ordered[start : start + WINDOW]

        async def nothing():
            return None

        exports = await _all(
            client.export(files[period]) if files[period] else nothing()
            for period in window
        )
        for period, export in zip(window, exports, strict=True):
            if files[period] is None:
                skipped[period] = "No dataset for the period"
                logger.debug(
                    "Indicator %s period %s skipped: no export", indicator_id, period
                )
            elif export is None:
                skipped[period] = f"Export file answered HTTP 404: {files[period]}"
                logger.warning(
                    "Indicator %s period %s skipped: export file answered HTTP 404",
                    indicator_id,
                    period,
                )
            else:
                try:
                    scan.add(period, export.text, export.url, export.last_modified)
                except ValueError as error:
                    logger.error(
                        "Indicator %s period %s export rejected: %s",
                        indicator_id,
                        period,
                        error,
                    )
                    raise ValueError(f"{period}: {error}") from error
                logger.debug(
                    "Indicator %s period %s: %d rows read",
                    indicator_id,
                    period,
                    scan.files[period].rows,
                )
    if not scan.files:
        raise Unreachable(f"Indicator {indicator_id} serves no export file")
    logger.debug(
        "Indicator %s scanned: %d of %d periods read, %d skipped, %d with "
        "conflicting cells",
        indicator_id,
        len(scan.files),
        len(files),
        len(skipped),
        len(scan.conflicts),
    )
    return scan, skipped


async def refresh(
    client, entry: Entry, catalogue: dict, stored: bool, known: dict, *, full, seen
):
    """Return ``(outcome, payload, signal)`` for one indicator.

    ``payload`` is ``{"responses", "derived"}`` for a rebuilt indicator and None
    when the stored entry is reused. Raises Unreachable when a resource retrieval
    depends on is unavailable; any other exception is a metadata failure. ``seen``
    receives the current links.
    """
    try:
        totals = await client.totals(entry.id)
        indicator = parse_totals(totals, entry.id)
    except LookupError as error:  # "Indicator has no datasets." and similar
        logger.warning("Indicator %s totals unavailable: %s", entry.id, error)
        raise Unreachable(str(error)) from error
    if not indicator.periods:
        raise Unreachable(f"Indicator {entry.id} lists no periods")
    logger.debug(
        "Indicator %s %r: %d periods (%s to %s)",
        entry.id,
        entry.name,
        len(indicator.periods),
        indicator.periods[0],
        indicator.periods[-1],
    )
    files = await export_links(client, indicator)
    seen.update(files=files)
    record = source_record(entry, indicator)
    signal = {
        "files": files,
        "source_digest": digest(record),
        "mapping_version": MAPPING_VERSION,
    }
    changed = [key for key, value in signal.items() if known.get(key) != value]
    if not full and stored and not changed:
        logger.debug(
            "Indicator %s reused: links, source record and mapping version "
            "unchanged; checking the newest export answers HEAD",
            entry.id,
        )
        await reachable(client, files)
        return "reused", None, signal
    if full:
        reason = "full run"
    elif not stored:
        reason = "no stored source entry"
    else:
        reason = "changed " + ", ".join(changed)
        old = known.get("files")
        if "files" in changed and isinstance(old, dict):
            added = len(files.keys() - old.keys())
            removed = len(old.keys() - files.keys())
            moved = sum(1 for p in files.keys() & old.keys() if files[p] != old[p])
            reason += (
                f" ({added} periods added, {removed} removed, {moved} links changed)"
            )
    # In a full run every indicator is rebuilt, which the run-level line states.
    logger.log(
        logging.DEBUG if full else logging.INFO,
        "Indicator %s %r rebuilt: %s",
        entry.id,
        entry.name,
        reason,
    )
    scan, skipped = await scan_exports(client, entry.id, files)
    payload = {
        "responses": {"catalogue": catalogue, "totals": totals, "links": files},
        "derived": {"scan": scan.to_summary(), "skipped_periods": skipped},
    }
    return "rebuilt", payload, {**signal, "skipped_periods": skipped}


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current catalogue rows."""
    connection = client or UkaClient(provider)
    try:
        entries = parse_catalogue(await connection.catalogue())
        logger.info("Discovery: catalogue lists %d indicators", len(entries))
        return [asdict(e) for e in entries]
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> dict:
    """Build one indicator's document in memory (typed boundary only)."""
    logger.debug("Fetching metadata for indicator %s", record.get("id"))
    entry = Entry(**record)
    catalogue = {
        "group": entry.group,
        "row": {
            "id": entry.id,
            "name": entry.name,
            "most_recent_url": entry.most_recent_url,
        },
    }
    _, payload, _ = await refresh(
        client, entry, catalogue, False, {}, full=True, seen={}
    )
    return document_from(payload["responses"], payload["derived"])


async def run(
    provider: dict,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    indicators=None,
    full=False,
) -> dict:
    """Refresh every (or each selected) indicator into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    logger.info(
        "UKÄ run started: %s run, indicators=%s, data=%s, output=%s",
        "full" if full else "refresh",
        sorted(map(str, indicators), key=int) if indicators else "all",
        data,
        output,
    )
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not _allowed_path(relative) for relative in baseline):
        raise ValueError("Unexpected file in published UKÄ data")
    state = load_state(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    store = SourceStore(output / "source", PROVIDER_CODE)
    # Placeholders stand in for documents: presence means a stored source entry.
    published, withheld = {}, {}
    for stored in store.entries():
        code = stored.split("/")[1]
        status = state["indicators"].get(code, {}).get("status")
        (withheld if status == "withheld" else published)[code] = True
    logger.info(
        "Loaded baseline: %d published, %d withheld, %d tracked indicators, "
        "state mapping version %s (current %s)",
        len(published),
        len(withheld),
        len(state["indicators"]),
        state.get("mapping_version"),
        MAPPING_VERSION,
    )
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    max_age = timedelta(days=provider.get("max_age_days", 60))
    cooldown = provider.get("retry_cooldown_seconds", 60)
    logger.info(
        "Effective options: max_age_days=%s, retry_cooldown_seconds=%s, concurrency=%s",
        max_age.days,
        cooldown,
        provider.get("concurrency", 2),
    )
    connection = client or UkaClient(provider)
    progress = Progress("uka", "uka")
    counts = dict.fromkeys(COUNTS, 0)
    errors = []

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        logger.error("%s failed for %s: %s", stage, resource, entry["error"])
        progress.error(stage, resource, "sv", entry["error"])
        return entry

    try:
        progress.update(phase="discovery", current="catalogue")
        problems, rows = [], {}
        catalogue_body = await connection.catalogue()
        entries, complete = catalogue_entries(catalogue_body, problems, rows)
        store.put_shared(
            PROVIDER_CODE, "indicators.json", catalogue_body, retrieved_at=stamp
        )
        for resource, error in problems:
            fail("discovery", resource, error)
        if not entries:
            raise ValueError("UKÄ's catalogue lists no indicators")
        listed = {str(entry.id) for entry in entries}
        if indicators is None:
            selected = entries
            logger.info("Selection: all %d catalogue indicators", len(selected))
        else:
            wanted = {str(code) for code in indicators}
            selected = [entry for entry in entries if str(entry.id) in wanted]
            for code in sorted(wanted - listed):
                fail("discovery", code, LookupError("Not in UKÄ's catalogue"))
            logger.info(
                "Selection: %d of %d catalogue indicators match the %d requested",
                len(selected),
                len(entries),
                len(wanted),
            )
        if complete and indicators is None:
            known = set(published) | set(withheld) | set(state["indicators"])
            for code in sorted(known - listed, key=int):
                state["indicators"].pop(code, None)
                store.remove(stored_key(code))
                if (published.pop(code, None) or withheld.pop(code, None)) is not None:
                    counts["removed"] += 1
                    logger.info(
                        "Indicator %s removed: absent from the complete catalogue",
                        code,
                    )
        elif indicators is not None:
            logger.info("Removal check skipped: the run is limited to a selection")
        else:
            logger.warning(
                "Removal check skipped: the catalogue was incomplete, so absent "
                "indicators are kept"
            )

        working: dict[str, bool] = {}
        progress.update(phase="metadata", total=len(selected), processed=0)
        pending, failures = list(selected), {}
        logger.info(
            "Refreshing %d indicators (%s)",
            len(pending),
            "full rebuild of every indicator"
            if full
            else "unchanged ones are reused after a HEAD check",
        )
        for attempt in (1, 2):
            retry = []
            if attempt == 2:
                logger.info("Attempt 2: retrying %d indicators", len(pending))
            for entry in pending:
                code = str(entry.id)
                progress.update(current=code, operation=f"refresh (attempt {attempt})")
                info = state["indicators"].setdefault(code, {})
                info["checked_at"] = stamp
                stored = store.get(stored_key(code)) is not None
                seen = {}
                try:
                    outcome, payload, signal = await refresh(
                        connection,
                        entry,
                        rows[entry.id],
                        stored,
                        info,
                        full=full,
                        seen=seen,
                    )
                    if payload is not None:
                        store.put(
                            stored_key(code),
                            responses=payload["responses"],
                            derived=payload["derived"],
                            updated=None,
                            retrieved_at=stamp,
                        )
                except Unreachable as error:
                    logger.warning(
                        "Indicator %s endpoint unavailable on attempt %d: %s",
                        code,
                        attempt,
                        error,
                    )
                    working[code] = False
                    failures[code] = ("endpoint", error)
                    retry.append(entry)
                    continue
                except Exception as error:
                    logger.warning(
                        "Indicator %s metadata failure on attempt %d: %s: %s",
                        code,
                        attempt,
                        type(error).__name__,
                        error,
                    )
                    failures[code] = ("metadata", error)
                    try:  # a mapping failure keeps a working endpoint published
                        await reachable(connection, seen.get("files") or info["files"])
                        working[code] = True
                    except Exception:
                        working[code] = False
                    logger.info(
                        "Indicator %s endpoint %s despite the metadata failure",
                        code,
                        "still works" if working[code] else "is not reachable",
                    )
                    continue
                failures.pop(code, None)
                working[code] = True
                counts[outcome] += 1
                info.update(signal, verified_at=stamp, error=None)
                info.setdefault("skipped_periods", {})
                (withheld if code in withheld else published)[code] = True
                progress.update(
                    processed=counts["rebuilt"] + counts["reused"],
                    succeeded=counts["rebuilt"],
                    reused=counts["reused"],
                )
            logger.info(
                "Attempt %d done: %d rebuilt, %d reused so far, %d to retry",
                attempt,
                counts["rebuilt"],
                counts["reused"],
                len(retry),
            )
            if not retry or attempt == 2:
                break
            logger.info("Waiting %ss before retrying unreachable indicators", cooldown)
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            pending = retry
        for code, (stage, error) in sorted(failures.items(), key=lambda i: int(i[0])):
            state["indicators"][code]["error"] = fail(stage, code, error)
            counts["failed"] += 1
        progress.update(failed=counts["failed"])

        was_withheld = set(withheld)
        settle(
            [str(entry.id) for entry in selected],
            published,
            withheld,
            state["indicators"],
            working,
            now,
            max_age,
            counts,
        )
        for code in sorted(set(withheld) - was_withheld, key=int):
            logger.warning(
                "Indicator %s withheld: %s",
                code,
                state["indicators"][code].get("withheld_reason"),
            )
        for code in sorted(was_withheld - set(withheld), key=int):
            if code in published:
                logger.info("Indicator %s restored to published", code)
    finally:
        if client is None:
            await connection.close()

    state["mapping_version"] = MAPPING_VERSION
    store.save()
    save(output, state)
    logger.info("Saved %d published source entries to %s", len(published), output)
    status = report_status(pending=0, errors=errors)
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "full": full,
        "selection": sorted(map(str, indicators), key=int) if indicators else None,
        "counts": counts,
        "published": len(published),
        "withheld": sorted(withheld, key=int),
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    logger.info(
        "UKÄ run finished: status=%s, counts=%s, %d published, %d withheld, %d errors",
        status,
        counts,
        len(published),
        len(withheld),
        len(errors),
    )
    return result


def collect(data: Path, output: Path) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info("Collecting UKÄ artifact %s into %s", output, data)
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale UKÄ artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("UKÄ artifact checksum mismatch")
    if not {STATE, REPORT} <= set(result["manifest"]) or not all(
        _allowed_path(relative) for relative in result["manifest"]
    ):
        raise ValueError("Unexpected UKÄ artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
        logger.debug("Applied %s", relative)
    for relative in harvest_manifest(data):
        if relative.startswith("source/") and relative not in result["manifest"]:
            (data / relative).unlink()
            logger.debug("Removed %s", relative)
    logger.info("Collected %d UKÄ files", len(result["manifest"]))
    return result

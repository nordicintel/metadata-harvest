"""Weekly UKÄ refresh with independent indicator failures.

Every run reads the catalogue, each indicator's totals and every period's export
link. An indicator whose links, source record and mapping version are unchanged
reuses its accepted document after a HEAD check of its newest export; any change
downloads all its exports transiently and rebuilds the document. Exports and
observations are never stored.

An indicator is published only while its value endpoint works in the current run
and its document was verified within ``max_age_days``; otherwise its last good
document is withheld in state and restored automatically when both hold again.
Only a complete, unbounded catalogue proves an indicator's removal.
"""

import asyncio
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from metadata_harvest.adapters.uka.client import UkaClient, Unreachable
from metadata_harvest.adapters.uka.mapping import (
    MAPPING_VERSION,
    Scan,
    build_dataset,
    source_record,
)
from metadata_harvest.adapters.uka.source import (
    Entry,
    Indicator,
    parse_catalogue,
    parse_totals,
)
from metadata_harvest.adapters.uka.state import (
    COLLECTION,
    REPORT,
    STATE,
    load_documents,
    load_state,
    save,
)
from metadata_harvest.artifacts import check_collection_sizes, manifest, report_status
from metadata_harvest.currency import settle
from metadata_harvest.progress import Progress
from metadata_harvest.storage import read, write
from metadata_harvest.validation import validate_metadata

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


def catalogue_entries(body, errors: list) -> tuple[list[Entry], bool]:
    """Parse rows independently; malformed or duplicate rows make it incomplete."""
    if not isinstance(body, dict):
        raise ValueError(f"unexpected UKÄ catalogue {str(body)[:200]!r}")
    complete, parsed = True, {}
    for group, items in body.items():
        if not isinstance(items, list):
            errors.append((group, ValueError("Catalogue group is not a list")))
            complete = False
            continue
        for item in items:
            try:
                (entry,) = parse_catalogue({group: [item]})
            except Exception as error:
                errors.append((str(item)[:200], error))
                complete = False
                continue
            parsed.setdefault(entry.id, []).append(entry)
    entries = []
    for identifier, found in parsed.items():
        if len(found) > 1:
            errors.append((str(identifier), ValueError("Listed more than once")))
            complete = False
        else:
            entries.append(found[0])
    return entries, complete


async def export_links(client, indicator: Indicator) -> dict[str, str | None]:
    """Each period's export file URL in UKÄ's period order (None: no export)."""
    links = await _all(client.export_link(indicator.id, p) for p in indicator.periods)
    return dict(zip(indicator.periods, links, strict=True))


async def reachable(client, files: dict[str, str | None]) -> None:
    """Raise unless the newest existing export file answers HEAD."""
    for period in reversed(list(files)):
        if files[period] and await client.available(files[period]):
            return
    raise Unreachable("No export file of the indicator is available")


async def scan_exports(client, indicator_id: int, files: dict[str, str | None]):
    """Read every period's export, newest first; returns the scan and skips."""
    scan, skipped = Scan(), {}
    ordered = list(reversed(list(files)))
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
            elif export is None:
                skipped[period] = f"Export file answered HTTP 404: {files[period]}"
            else:
                try:
                    scan.add(period, export.text, export.url, export.last_modified)
                except ValueError as error:
                    raise ValueError(f"{period}: {error}") from error
    if not scan.files:
        raise Unreachable(f"Indicator {indicator_id} serves no export file")
    return scan, skipped


async def refresh(client, entry: Entry, accepted, known: dict, *, full: bool, seen):
    """Return ``(outcome, document, signal)`` for one indicator.

    Raises Unreachable when a resource retrieval depends on is unavailable; any
    other exception is a metadata failure. ``seen`` receives the current links.
    """
    try:
        indicator = parse_totals(await client.totals(entry.id), entry.id)
    except LookupError as error:  # "Indicator has no datasets." and similar
        raise Unreachable(str(error)) from error
    if not indicator.periods:
        raise Unreachable(f"Indicator {entry.id} lists no periods")
    files = await export_links(client, indicator)
    seen.update(files=files)
    record = source_record(entry, indicator)
    signal = {
        "files": files,
        "source_digest": digest(record),
        "mapping_version": MAPPING_VERSION,
    }
    if (
        not full
        and accepted is not None
        and all(known.get(key) == value for key, value in signal.items())
    ):
        await reachable(client, files)
        return "reused", accepted, signal
    scan, skipped = await scan_exports(client, entry.id, files)
    document = build_dataset(entry, indicator, scan)
    validate_metadata(document, adapter="uka")
    return "rebuilt", document, {**signal, "skipped_periods": skipped}


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current catalogue rows."""
    connection = client or UkaClient(provider)
    try:
        return [asdict(e) for e in parse_catalogue(await connection.catalogue())]
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> dict:
    """Build and validate one indicator's document from a catalogue row."""
    outcome, document, _ = await refresh(
        client, Entry(**record), None, {}, full=True, seen={}
    )
    return document


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
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - {COLLECTION, STATE, REPORT}:
        raise ValueError("Unexpected file in published UKÄ data")
    state, published = load_state(data), load_documents(data)
    withheld = state["withheld"]
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    max_age = timedelta(days=provider.get("max_age_days", 60))
    cooldown = provider.get("retry_cooldown_seconds", 60)
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
        progress.error(stage, resource, "sv", entry["error"])
        return entry

    try:
        progress.update(phase="discovery", current="catalogue")
        problems = []
        entries, complete = catalogue_entries(await connection.catalogue(), problems)
        for resource, error in problems:
            fail("discovery", resource, error)
        if not entries:
            raise ValueError("UKÄ's catalogue lists no indicators")
        listed = {str(entry.id) for entry in entries}
        if indicators is None:
            selected = entries
        else:
            wanted = {str(code) for code in indicators}
            selected = [entry for entry in entries if str(entry.id) in wanted]
            for code in sorted(wanted - listed):
                fail("discovery", code, LookupError("Not in UKÄ's catalogue"))
        if complete and indicators is None:
            known = set(published) | set(withheld) | set(state["indicators"])
            for code in sorted(known - listed, key=int):
                state["indicators"].pop(code, None)
                if (published.pop(code, None) or withheld.pop(code, None)) is not None:
                    counts["removed"] += 1

        working: dict[str, bool] = {}
        progress.update(phase="metadata", total=len(selected), processed=0)
        pending, failures = list(selected), {}
        for attempt in (1, 2):
            retry = []
            for entry in pending:
                code = str(entry.id)
                progress.update(current=code, operation=f"refresh (attempt {attempt})")
                info = state["indicators"].setdefault(code, {})
                info["checked_at"] = stamp
                accepted = published.get(code) or withheld.get(code)
                seen = {}
                try:
                    outcome, document, signal = await refresh(
                        connection, entry, accepted, info, full=full, seen=seen
                    )
                except Unreachable as error:
                    working[code] = False
                    failures[code] = ("endpoint", error)
                    retry.append(entry)
                    continue
                except Exception as error:
                    failures[code] = ("metadata", error)
                    try:  # a mapping failure keeps a working endpoint published
                        await reachable(connection, seen.get("files") or info["files"])
                        working[code] = True
                    except Exception:
                        working[code] = False
                    continue
                failures.pop(code, None)
                working[code] = True
                counts[outcome] += 1
                info.update(signal, verified_at=stamp, error=None)
                info.setdefault("skipped_periods", {})
                (withheld if code in withheld else published)[code] = document
                progress.update(
                    processed=counts["rebuilt"] + counts["reused"],
                    succeeded=counts["rebuilt"],
                    reused=counts["reused"],
                )
            if not retry or attempt == 2:
                break
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            pending = retry
        for code, (stage, error) in sorted(failures.items(), key=lambda i: int(i[0])):
            state["indicators"][code]["error"] = fail(stage, code, error)
            counts["failed"] += 1
        progress.update(failed=counts["failed"])

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
    finally:
        if client is None:
            await connection.close()

    state["mapping_version"] = MAPPING_VERSION
    save(output, state, published)
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
    return result


def collect(data: Path, output: Path) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale UKÄ artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("UKÄ artifact checksum mismatch")
    if set(result["manifest"]) != {COLLECTION, STATE, REPORT}:
        raise ValueError("Unexpected UKÄ artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    return result

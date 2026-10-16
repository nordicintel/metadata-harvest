"""Weekly UKÄ refresh with independent indicator failures.

The catalogue is the one scope. Every run reads each indicator's totals and every
period's export link. An indicator whose links, source record and mapping version
are unchanged keeps its stored source entry after a HEAD check of its newest
export; any change (or a full run) downloads all its exports transiently and
stores the catalogue row, totals and links as responses plus the scan summary the
mapping consumes as derived data. Exports and observations are never stored; an
export that cannot be scanned is a metadata failure that keeps the stored entry.
"""

import asyncio
import hashlib
import json

from apps.harvest.metadata_adapters.uka.client import UkaClient, Unreachable
from apps.harvest.metadata_adapters.uka.mapping import (
    MAPPING_VERSION,
    Scan,
    source_record,
)
from apps.harvest.metadata_adapters.uka.source import (
    Entry,
    Indicator,
    parse_catalogue,
    parse_totals,
)
from harvest.lifecycle import Adapter, Kept, Run, Scope, Stored, Task

PROVIDER_CODE = "uka"
WINDOW = 4
"""Most export files of one indicator downloaded at once."""


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


def catalogue_entries(body, errors: list, rows: dict) -> tuple[list[Entry], bool]:
    """Parse rows independently; malformed or duplicate rows make it incomplete.

    ``rows`` receives each usable indicator's raw catalogue row and group.
    """
    if not isinstance(body, dict):
        raise ValueError(f"unexpected UKÄ catalogue {str(body)[:200]!r}")
    complete, parsed, raw = True, {}, {}
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
            raw[entry.id] = {"group": group, "row": item}
    entries = []
    for identifier, found in parsed.items():
        if len(found) > 1:
            errors.append((str(identifier), ValueError("Listed more than once")))
            complete = False
        else:
            entries.append(found[0])
            rows[found[0].id] = raw[found[0].id]
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


async def discover(run: Run) -> list[Scope]:
    scope = Scope("catalogue", None)

    async def load() -> list[Task]:
        body = await run.client.catalogue()
        problems, rows = [], {}
        entries, complete = catalogue_entries(body, problems, rows)
        run.store.put_shared(
            PROVIDER_CODE, "indicators.json", body, retrieved_at=run.stamp
        )
        for resource, error in problems:
            run.fail("discovery", resource, error)
        if not entries:
            raise ValueError("UKÄ's catalogue lists no indicators")
        wanted = run.selection.get("indicators")
        if wanted is not None:
            wanted = {str(code) for code in wanted}
            for code in sorted(wanted - {str(e.id) for e in entries}):
                run.fail("discovery", f"*/{code}", LookupError("Unknown UKÄ indicator"))
        scope.complete = complete
        run.state["mapping_version"] = MAPPING_VERSION
        return [
            Task(f"sv/{entry.id}", (entry, rows[entry.id]))
            for entry in entries
            if wanted is None or str(entry.id) in wanted
        ]

    scope.load = load
    return [scope]


async def fetch(run: Run, task: Task) -> Stored | Kept:
    entry, catalogue = task.item
    info = run.records.get(task.key, {})
    stored = run.store.get(run.stored_key(task.key)) is not None
    try:
        totals = await run.client.totals(entry.id)
        indicator = parse_totals(totals, entry.id)
    except LookupError as error:  # "Indicator has no datasets." and similar
        raise Unreachable(str(error)) from error
    if not indicator.periods:
        raise Unreachable(f"Indicator {entry.id} lists no periods")
    files = await export_links(run.client, indicator)
    signal = {
        "files": files,
        "source_digest": digest(source_record(entry, indicator)),
        "mapping_version": MAPPING_VERSION,
    }
    changed = [key for key, value in signal.items() if info.get(key) != value]
    if not run.selection.get("full") and stored and not changed:
        await reachable(run.client, files)
        return Kept(info=signal)
    scan, skipped = await scan_exports(run.client, entry.id, files)
    return Stored(
        responses={"catalogue": catalogue, "totals": totals, "links": files},
        derived={"scan": scan.to_summary(), "skipped_periods": skipped},
        info={**signal, "skipped_periods": skipped},
    )


ADAPTER = Adapter(
    name="uka",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-uka.yml",
    client=lambda binding: UkaClient(binding.options, base_url=binding.endpoint_url),
    discover=discover,
    fetch=fetch,
    options={"indicator": "indicators", "full": "full"},
    languages=("sv",),
    collection="indicators",
)

"""Weekly Tillväxtverket refresh with independent dataset failures.

There is no listing endpoint: the datasets are the hard-coded registry in
:mod:`.source`, and each dataset is its own scope. Every run checks every
(selected) dataset with two small requests, its columns/``rowCount``
(``limit=0``) and its distinct ``LOAD_DATE`` values; together they are the change
signal and the endpoint check. An unchanged signal reuses the accepted cube
without distinct-value queries; otherwise the cube is rebuilt (one distinct query
per dimension plus the column label file). The data.europa.eu record (title,
description, contact) is fetched every run and re-applied; when it fails, the
last accepted record is kept and the failure is reported as a dependency error.

A dataset is published only while its endpoint works and its document was
verified within ``max_age_days``; otherwise its last good document is withheld in
state. Datasets are never removed because a request failed: only an unbounded
run removes documents whose dataset is no longer in the registry (a code change).
"""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from metadata_harvest.adapters.tillvaxtverket.api import (
    cbase_url,
    dcat_url,
    labels_url,
    metadata_url,
)
from metadata_harvest.adapters.tillvaxtverket.client import (
    TillvaxtverketClient,
    Unreachable,
)
from metadata_harvest.adapters.tillvaxtverket.mapping import (
    MAPPING_VERSION,
    apply_dcat,
    build_cube,
    previous_dcat,
    previous_labels,
)
from metadata_harvest.adapters.tillvaxtverket.source import (
    DATASETS,
    LOAD_DATE,
    REGION_DISTINCT,
    Conflict,
    Probe,
    Spec,
    parse_categories,
    parse_dcat,
    parse_labels,
    parse_periods,
    parse_probe,
    parse_regions,
)
from metadata_harvest.adapters.tillvaxtverket.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    collection,
    load_documents,
    load_state,
    save,
)
from metadata_harvest.artifacts import check_collection_sizes, manifest, report_status
from metadata_harvest.currency import settle
from metadata_harvest.progress import Progress
from metadata_harvest.storage import read, write
from metadata_harvest.validation import validate_metadata

ADAPTER = "tillvaxtverket"
LANGUAGES = ("sv",)
DISTINCT_LIMIT = 100_000
"""``limit`` of distinct-value queries (the largest list, days, has ~4,300 rows)."""
LABELS_ENCODING = "cp1252"
"""The label files are Latin-1 (Windows-1252) text without a charset header."""
COUNTS = (
    "rebuilt",
    "relabelled",
    "unchanged",
    "failed",
    "dependency_failed",
    "quarantined",
    "withheld",
    "restored",
    "removed",
)


def key_of(spec: Spec) -> str:
    return f"sv/{spec.dataset_code}"


class _Dataset:
    """Requests and builds for one registered dataset."""

    def __init__(self, client, spec: Spec):
        self.client, self.spec = client, spec
        self.url = cbase_url(spec.area, spec.name)

    async def distinct(self, *columns: str):
        params = [("dimension", column) for column in columns]
        params += [("format", "json"), ("limit", str(DISTINCT_LIMIT))]
        return await self.client.get_json(self.url, params)

    async def probe(self) -> Probe:
        """The change signal; Unreachable means the endpoint failed."""
        columns = await self.client.get_json(
            self.url, [("format", "json"), ("limit", "0")]
        )
        loads = await self.distinct(LOAD_DATE)
        return parse_probe(self.spec, columns, loads)

    async def dcat(self) -> dict:
        return parse_dcat(await self.client.get_json(dcat_url(self.spec.name)))

    async def labels(self) -> dict[str, str]:
        content = await self.client.get_text(
            labels_url(self.spec.area, self.spec.name), encoding=LABELS_ENCODING
        )
        return parse_labels(content)

    async def cube(self, probe: Probe, labels: dict[str, str] | None) -> dict:
        regions = parse_regions(await self.distinct(*REGION_DISTINCT))
        categories = {}
        for dimension in self.spec.dimensions:
            body = await self.distinct(dimension.column, *dimension.label_columns)
            categories[dimension.id] = parse_categories(dimension, body)
        periods = parse_periods(self.spec, await self.distinct(self.spec.time))
        return build_cube(self.spec, probe, regions, categories, periods, labels)


async def discover_provider(provider: dict, *, client=None) -> list[Spec]:
    """The registry: there is no listing endpoint to ask (no requests)."""
    return list(DATASETS)


async def fetch_metadata(spec: Spec, *, client) -> dict:
    """Build and validate one dataset's document from scratch (live checks)."""
    dataset = _Dataset(client, spec)
    probe = await dataset.probe()
    document = apply_dcat(
        await dataset.cube(probe, await dataset.labels()), spec, await dataset.dcat()
    )
    validate_metadata(document, adapter=ADAPTER)
    return document


def select(products) -> tuple[list[Spec], list[str]]:
    """Registry entries named by dataset name or code (case-insensitive), and
    the names that match none."""
    if products is None:
        return list(DATASETS), []
    wanted = {name.casefold(): name for name in products}
    selected = [
        spec
        for spec in DATASETS
        if {spec.name.casefold(), spec.dataset_code.casefold()} & wanted.keys()
    ]
    names = {k.casefold() for s in DATASETS for k in (s.name, s.dataset_code)}
    return selected, [wanted[k] for k in sorted(wanted.keys() - names)]


async def run(
    provider: dict,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    products=None,
    languages=None,
) -> dict:
    """Check every (or each selected) dataset into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(configured) - set(LANGUAGES) or set(selected_languages) - set(configured):
        raise ValueError(f"Tillväxtverket is Swedish only: {selected_languages}")
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published Tillväxtverket data")
    state = load_state(data)
    published = load_documents(data, configured)
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
    connection = client or TillvaxtverketClient()
    progress = Progress(ADAPTER, PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors: list[dict] = []
    report: dict = {"quarantined": []}

    def fail(stage, resource, error, url=None):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        if url:
            entry["url"] = url
        errors.append(entry)
        progress.error(stage, resource, "sv", entry["error"], url=url)
        return entry

    def known():
        return set(published) | set(withheld) | set(state["datasets"])

    try:
        selected, unknown = select(products)
        for name in unknown:
            fail("discovery", name, LookupError("Unknown Tillväxtverket dataset"))
        if products is None:
            current = {key_of(spec) for spec in DATASETS}
            for key in sorted(known() - current):
                state["datasets"].pop(key, None)
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1

        working: dict[str, bool] = {}
        failures: dict[str, tuple] = {}
        dependencies: dict[str, tuple] = {}
        outcome: dict[str, str] = {}
        settled: set[str] = set()

        async def harvest(spec: Spec) -> bool:
            """Check one dataset; return whether its endpoint failed."""
            key = key_of(spec)
            dataset = _Dataset(connection, spec)
            settled.add(key)
            info = state["datasets"].setdefault(key, {})
            info["checked_at"] = stamp
            accepted = published.get(key) or withheld.get(key)
            progress.update(current=spec.name, operation="probe")
            try:
                probe = await dataset.probe()
            except Unreachable as error:
                working[key], failures[key] = False, ("endpoint", error, dataset.url)
                return True
            except Exception as error:  # the endpoint answered
                working[key], failures[key] = True, ("metadata", error, dataset.url)
                return False
            working[key] = True
            try:
                dcat = await dataset.dcat()
                dependencies.pop(f"{key}#data.europa.eu", None)
            except Exception as error:
                dcat = previous_dcat(accepted)
                dependencies[f"{key}#data.europa.eu"] = (error, dcat_url(spec.name))
            reuse = (
                accepted is not None
                and info.get("mapping_version") == MAPPING_VERSION
                and not info.get("rebuild")
                and accepted["extension"][PROVIDER_CODE].get("source_record")
                == probe.signal()
            )
            labels_missing = False
            try:
                if reuse:
                    cube = accepted
                else:
                    progress.update(operation="rebuild")
                    try:
                        labels = await dataset.labels()
                        dependencies.pop(f"{key}#labels", None)
                    except Exception as error:
                        labels, labels_missing = previous_labels(accepted), True
                        dependencies[f"{key}#labels"] = (
                            error,
                            labels_url(spec.area, spec.name),
                        )
                    cube = await dataset.cube(probe, labels)
                document = apply_dcat(cube, spec, dcat)
                validate_metadata(document, adapter=ADAPTER)
            except Unreachable as error:
                working[key], failures[key] = False, ("endpoint", error, dataset.url)
                return True
            except Exception as error:
                failures[key] = ("metadata", error, metadata_url(spec.area, spec.name))
                if isinstance(error, Conflict):
                    outcome[key] = "quarantined"
                return False
            failures.pop(key, None)
            outcome.pop(key, None)
            if document == accepted:
                outcome[key] = "unchanged"
            else:
                outcome[key] = "relabelled" if reuse else "rebuilt"
            info.update(
                verified_at=stamp,
                mapping_version=MAPPING_VERSION,
                rebuild=labels_missing,
                error=None,
            )
            (withheld if key in withheld else published)[key] = document
            return False

        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, spec in enumerate(selected, start=1):
            if await harvest(spec):
                retry.append(spec)
            progress.update(processed=number)
        if retry:
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for spec in retry:
                progress.update(current=spec.name, operation="retry")
                await harvest(spec)
        for key, (stage, error, url) in sorted(failures.items()):
            entry = fail(stage, key, error, url)
            counts["failed"] += 1
            if outcome.get(key) == "quarantined":
                counts["quarantined"] += 1
                report["quarantined"].append(key)
            state["datasets"][key]["error"] = entry
        for resource, (error, url) in sorted(dependencies.items()):
            fail("dependency", resource, error, url)
            counts["dependency_failed"] += 1
        for key, result in outcome.items():
            if key not in failures and result in counts:
                counts[result] += 1
        progress.update(failed=counts["failed"])
        settle(
            sorted(settled),
            published,
            withheld,
            state["datasets"],
            working,
            now,
            max_age,
            counts,
        )
    finally:
        if client is None:
            await connection.close()

    state["mapping_version"] = MAPPING_VERSION
    save(output, state, published, configured)
    status = report_status(pending=0, errors=errors)
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "selection": {"products": products, "languages": selected_languages},
        "counts": counts,
        "published": {
            language: sum(k.startswith(f"{language}/") for k in published)
            for language in configured
        },
        "withheld": sorted(withheld),
        "requests": getattr(connection, "request_count", None),
        **report,
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale Tillväxtverket artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Tillväxtverket artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        raise ValueError("Unexpected Tillväxtverket artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    return result

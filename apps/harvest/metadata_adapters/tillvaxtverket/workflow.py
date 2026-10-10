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
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.tillvaxtverket.api import (
    cbase_url,
    dcat_url,
    labels_url,
    metadata_url,
)
from apps.harvest.metadata_adapters.tillvaxtverket.client import (
    TillvaxtverketClient,
    Unreachable,
)
from apps.harvest.metadata_adapters.tillvaxtverket.mapping import (
    MAPPING_VERSION,
    apply_dcat,
    build_cube,
    previous_dcat,
    previous_labels,
)
from apps.harvest.metadata_adapters.tillvaxtverket.source import (
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
from apps.harvest.metadata_adapters.tillvaxtverket.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    collection,
    load_documents,
    load_state,
    save,
)
from apps.harvest.storage import read, write
from apps.harvest.validation import validate_metadata
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest

logger = logging.getLogger("tillvaxtverket_logger")

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
        logger.debug("%s: distinct values of %s", self.spec.dataset_code, columns)
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
        cube = build_cube(self.spec, probe, regions, categories, periods, labels)
        logger.info(
            "%s: cube built from %d regions, %s categories and %d periods (%s labels)",
            self.spec.dataset_code,
            len(regions),
            {name: len(found) for name, found in categories.items()},
            len(periods),
            "with" if labels is not None else "without",
        )
        return cube


async def discover_provider(provider: dict, *, client=None) -> list[Spec]:
    """The registry: there is no listing endpoint to ask (no requests)."""
    logger.info(
        "Discovery: no listing endpoint, using the %d registered datasets",
        len(DATASETS),
    )
    return list(DATASETS)


async def fetch_metadata(spec: Spec, *, client) -> dict:
    """Build and validate one dataset's document from scratch (live checks)."""
    logger.info(
        "%s: building the document from scratch (live check)", spec.dataset_code
    )
    dataset = _Dataset(client, spec)
    probe = await dataset.probe()
    document = apply_dcat(
        await dataset.cube(probe, await dataset.labels()), spec, await dataset.dcat()
    )
    validate_metadata(document, adapter=ADAPTER)
    logger.info("%s: document built and validated", spec.dataset_code)
    return document


def select(products) -> tuple[list[Spec], list[str]]:
    """Registry entries named by dataset name or code (case-insensitive), and
    the names that match none."""
    if products is None:
        logger.info("Selection: no product filter, all %d datasets", len(DATASETS))
        return list(DATASETS), []
    wanted = {name.casefold(): name for name in products}
    selected = [
        spec
        for spec in DATASETS
        if {spec.name.casefold(), spec.dataset_code.casefold()} & wanted.keys()
    ]
    names = {k.casefold() for s in DATASETS for k in (s.name, s.dataset_code)}
    unknown = [wanted[k] for k in sorted(wanted.keys() - names)]
    logger.info(
        "Selection: product filter %s matched %d of %d datasets (%d unknown names)",
        sorted(wanted.values()),
        len(selected),
        len(DATASETS),
        len(unknown),
    )
    for spec in DATASETS:
        if spec not in selected:
            logger.debug(
                "Skipping %s: not named by the product filter", spec.dataset_code
            )
    if unknown:
        logger.warning("Unknown dataset names in product filter: %s", unknown)
    return selected, unknown


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
    logger.info(
        "Baseline in %s: %d published documents, %d withheld, %d datasets in state "
        "(state mapping version %s, current %s)",
        data,
        len(published),
        len(withheld),
        len(state["datasets"]),
        state.get("mapping_version"),
        MAPPING_VERSION,
    )
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
    logger.info(
        "Run start: %s, products=%s, languages=%s, max age %s, retry cooldown %ss, "
        "baseline %d files, output %s",
        "refresh of a published baseline" if baseline else "first run (no baseline)",
        products if products is not None else "all",
        selected_languages,
        max_age,
        cooldown,
        len(baseline),
        output,
    )
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
        logger.error(
            "%s failure for %s: %s (%s)",
            stage,
            resource,
            entry["error"],
            url or "no url",
        )
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
            gone = sorted(known() - current)
            logger.info(
                "Unbounded run: %d known datasets, %d no longer in the registry",
                len(known()),
                len(gone),
            )
            for key in gone:
                state["datasets"].pop(key, None)
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1
                    logger.warning("Removed %s: no longer in the registry", key)
        else:
            logger.info(
                "Bounded run: removal of datasets missing from the registry is "
                "skipped, %d known datasets kept untouched",
                len(known()),
            )

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
            if key in withheld:
                previously = "withheld"
            elif key in published:
                previously = "published"
            else:
                previously = "not accepted"
            logger.info("%s: probing (previously %s)", key, previously)
            try:
                probe = await dataset.probe()
            except Unreachable as error:
                working[key], failures[key] = False, ("endpoint", error, dataset.url)
                logger.warning(
                    "%s: probe could not reach the endpoint; will be retried once "
                    "after the other datasets (%s)",
                    key,
                    error,
                )
                return True
            except Exception as error:  # the endpoint answered
                working[key], failures[key] = True, ("metadata", error, dataset.url)
                logger.warning(
                    "%s: endpoint answered but the probe was rejected, keeping the "
                    "last accepted document: %s: %s",
                    key,
                    type(error).__name__,
                    error,
                )
                return False
            working[key] = True
            logger.info(
                "%s: probe ok, %d rows, %d columns, latest load %s",
                key,
                probe.row_count,
                len(probe.columns),
                probe.loaded,
            )
            try:
                dcat = await dataset.dcat()
                dependencies.pop(f"{key}#data.europa.eu", None)
            except Exception as error:
                dcat = previous_dcat(accepted)
                dependencies[f"{key}#data.europa.eu"] = (error, dcat_url(spec.name))
                logger.warning(
                    "%s: data.europa.eu record unavailable (%s: %s); %s",
                    key,
                    type(error).__name__,
                    error,
                    "reusing the last accepted record"
                    if dcat
                    else "continuing without title, description and contacts",
                )
            if accepted is None:
                reason = "no accepted document yet"
            elif info.get("mapping_version") != MAPPING_VERSION:
                reason = (
                    f"mapping version changed ({info.get('mapping_version')} "
                    f"-> {MAPPING_VERSION})"
                )
            elif info.get("rebuild"):
                reason = "previous build lacked the label file"
            else:
                reason = "change signal moved (columns, row count or load dates)"
            reuse = (
                accepted is not None
                and info.get("mapping_version") == MAPPING_VERSION
                and not info.get("rebuild")
                and accepted["extension"][PROVIDER_CODE].get("source_record")
                == probe.signal()
            )
            labels_missing = False
            if reuse:
                logger.info(
                    "%s: change signal unchanged, reusing the accepted cube "
                    "(skipping distinct-value queries and the label file)",
                    key,
                )
            else:
                logger.info("%s: rebuilding the cube: %s", key, reason)
            try:
                if reuse:
                    cube = accepted
                else:
                    progress.update(operation="rebuild")
                    try:
                        labels = await dataset.labels()
                        dependencies.pop(f"{key}#labels", None)
                        logger.debug("%s: %d column labels read", key, len(labels))
                    except Exception as error:
                        labels, labels_missing = previous_labels(accepted), True
                        dependencies[f"{key}#labels"] = (
                            error,
                            labels_url(spec.area, spec.name),
                        )
                        logger.warning(
                            "%s: label file unavailable (%s: %s); %s",
                            key,
                            type(error).__name__,
                            error,
                            "using labels from the last accepted document"
                            if labels
                            else "labels fall back to column names",
                        )
                    cube = await dataset.cube(probe, labels)
                document = apply_dcat(cube, spec, dcat)
                validate_metadata(document, adapter=ADAPTER)
            except Unreachable as error:
                working[key], failures[key] = False, ("endpoint", error, dataset.url)
                logger.warning(
                    "%s: endpoint failed while building; will be retried once "
                    "after the other datasets (%s)",
                    key,
                    error,
                )
                return True
            except Exception as error:
                failures[key] = ("metadata", error, metadata_url(spec.area, spec.name))
                if isinstance(error, Conflict):
                    outcome[key] = "quarantined"
                logger.warning(
                    "%s: build rejected%s, keeping the last accepted document: %s: %s",
                    key,
                    " (quarantined, conflicting identities)"
                    if isinstance(error, Conflict)
                    else "",
                    type(error).__name__,
                    error,
                )
                return False
            failures.pop(key, None)
            outcome.pop(key, None)
            if document == accepted:
                outcome[key] = "unchanged"
            else:
                outcome[key] = "relabelled" if reuse else "rebuilt"
            logger.info("%s: accepted as %s", key, outcome[key])
            info.update(
                verified_at=stamp,
                mapping_version=MAPPING_VERSION,
                rebuild=labels_missing,
                error=None,
            )
            (withheld if key in withheld else published)[key] = document
            return False

        logger.info(
            "Checking %d of %d registered datasets (every selected dataset is "
            "probed each run; there is no checkpoint or budget)",
            len(selected),
            len(DATASETS),
        )
        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, spec in enumerate(selected, start=1):
            logger.debug("Dataset %d/%d: %s", number, len(selected), spec.dataset_code)
            if await harvest(spec):
                retry.append(spec)
            progress.update(processed=number)
        logger.info(
            "First pass done: %d checked, %d with endpoint failures to retry",
            len(selected),
            len(retry),
        )
        if retry:
            logger.info(
                "Retrying %d dataset(s) after a %ss cooldown: %s",
                len(retry),
                cooldown,
                [spec.dataset_code for spec in retry],
            )
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for spec in retry:
                progress.update(current=spec.name, operation="retry")
                failed = await harvest(spec)
                logger.info(
                    "%s: retry %s",
                    key_of(spec),
                    "failed again" if failed else "reached the endpoint",
                )
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
        before = (set(published), set(withheld))
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
        for key in sorted(set(published) - before[0]):
            logger.info("%s: restored to published (endpoint working, fresh)", key)
        for key in sorted(set(withheld) - before[1]):
            logger.warning(
                "%s: withheld (%s)",
                key,
                state["datasets"][key].get("withheld_reason"),
            )
        statuses = [state["datasets"][key].get("status") for key in settled]
        logger.info(
            "Settled %d datasets (published while the endpoint works and the "
            "document was verified within %s): %d published, %d withheld, "
            "%d without any accepted document",
            len(settled),
            max_age,
            statuses.count("published"),
            statuses.count("withheld"),
            statuses.count("missing"),
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
    logger.info(
        "Run finished: status %s, %s requests, %d published, %d withheld, "
        "%d errors, counts %s",
        status,
        result["requests"],
        sum(result["published"].values()),
        len(withheld),
        len(errors),
        counts,
    )
    progress.update(phase="finished", result_status=status, current=None)
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    logger.info("Collecting worker output %s into %s", output, data)
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
        logger.debug("Applied %s", relative)
    logger.info(
        "Collected %d files (status %s)", len(result["manifest"]), result["status"]
    )
    return result

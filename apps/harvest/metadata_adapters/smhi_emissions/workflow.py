"""Monthly rebuild of Nationella emissionsdatabasen with independent substance failures.

One ``getfilteroptions/`` request discovers the sector tree and the substances; it
failing fails the whole run, which then publishes nothing. Each substance's national
``getdata`` probe supplies its years and submission round and is its endpoint check.
A full run is 30 requests, so every run refetches every substance; an identical
probe keeps its stored bytes. The tree is stored as a shared source file and each
probe as its substance's envelope. A substance is published only while its probe
answers and its entry was verified within ``max_age_days``; otherwise it is
withheld in state while its source files remain. Only a complete tree in an
unbounded run proves a substance's removal. Documents are built later by ``parse``.
"""

import asyncio
import logging
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.smhi_emissions.api import (
    DATA_URL,
    FILTER_OPTIONS_URL,
    dataset_code,
    probe_parameters,
)
from apps.harvest.metadata_adapters.smhi_emissions.client import SmhiClient, Unreachable
from apps.harvest.metadata_adapters.smhi_emissions.mapping import (
    MAPPING_VERSION,
    build_dataset,
)
from apps.harvest.metadata_adapters.smhi_emissions.source import (
    FilterOptions,
    Substance,
    parse_filter_options,
    parse_probe,
)
from apps.harvest.metadata_adapters.smhi_emissions.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    load_state,
    save,
)
from apps.harvest.source_store import (
    Rejected,
    SourceStore,
    harvest_manifest,
    source_key,
    split_key,
)
from apps.harvest.storage import read, write
from apps.operations.progress import Progress
from apps.publication.artifacts import check_collection_sizes, manifest
from registry import ServiceBinding
from retrieval.data_adapters.smhi_emissions import DATASET_PREFIX

ADAPTER = "smhi_emissions"
LANGUAGES = ("sv",)
COUNTS = ("rebuilt", "unchanged", "failed", "withheld", "restored", "removed")

logger = logging.getLogger("smhi_emissions_logger")


def stored_key(key: str) -> str:
    """The source-store key of a ``<language>/<dataset_code>`` state key."""
    language, code = key.split("/", 1)
    return source_key(PROVIDER_CODE, code, language)


def state_key(key: str) -> str:
    _, code, language = split_key(key)
    return f"{language}/{code}"


def _allowed_path(relative: str) -> bool:
    return relative in {STATE, REPORT} or relative.startswith("source/")


def submission_of(body) -> int | None:
    """``primary_submission`` read straight from the probe body, else None."""
    value = body.get("primary_submission") if isinstance(body, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def probe_substance(client, substance: Substance):
    """The raw national probe body of one substance.

    Raises:
        Unreachable: The probe failed (an endpoint failure).
    """
    logger.debug("Probing substance %s (%s)", substance.code, substance.long_name)
    return await client.get_json(DATA_URL, probe_parameters(substance.code))


async def build_substance(client, substance: Substance, options: FilterOptions):
    """Probe one substance and build its document in memory (typed boundary)."""
    body = await probe_substance(client, substance)
    probe = parse_probe(body, options, substance.code)
    return build_dataset(substance, options, probe), probe.submission


async def discover_provider(binding: ServiceBinding, *, client=None) -> list[dict]:
    """The current substances, as ``{"code", "substance_id", "long_name"}``."""
    connection = client or SmhiClient()
    try:
        options = parse_filter_options(await connection.get_json(FILTER_OPTIONS_URL))
    finally:
        if client is None:
            await connection.close()
    logger.info(
        "Discovered %d substances (%d quarantined, %d malformed entries)",
        len(options.substances),
        len(options.quarantined),
        len(options.problems),
    )
    return [
        {"code": s.code, "substance_id": s.substance_id, "long_name": s.long_name}
        for s in options.substances.values()
    ]


async def fetch_metadata(record: dict, *, client) -> dict:
    """Build and validate the Swedish document of ``{"code": <substance code>}``."""
    options = parse_filter_options(await client.get_json(FILTER_OPTIONS_URL))
    if record["code"] not in options.substances:
        logger.warning(
            "Substance %s is not in the current getfilteroptions tree (%d listed)",
            record["code"],
            len(options.substances),
        )
        raise LookupError(f"Unknown SMHI emissions substance {record['code']!r}")
    logger.info("Building single document for substance %s", record["code"])
    document, _ = await build_substance(
        client, options.substances[record["code"]], options
    )
    return document


def _select(options: FilterOptions, subjects) -> tuple[list[str], list[str]]:
    """Selected substance codes (exact, else case-insensitive) and unknown names."""
    listed = [*options.substances, *options.quarantined]
    if subjects is None:
        logger.info(
            "No subject filter: selecting all %d listed substances", len(listed)
        )
        return listed, []
    selected, unknown = [], []
    for name in subjects:
        matches = [c for c in listed if c == name] or [
            c for c in listed if c.casefold() == name.casefold()
        ]
        if len(matches) == 1:
            selected.append(matches[0])
            logger.debug("Subject %r matched substance %s", name, matches[0])
        else:
            unknown.append(name)
            logger.warning(
                "Subject %r matches %d listed substances; treated as unknown",
                name,
                len(matches),
            )
    chosen = list(dict.fromkeys(selected))
    logger.info(
        "Subject filter: selected %d of %d listed substances; %d unknown%s",
        len(chosen),
        len(listed),
        len(unknown),
        f" ({unknown})" if unknown else "",
    )
    return chosen, unknown


async def run(
    binding: ServiceBinding,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    subjects=None,
    languages=None,
) -> dict:
    """Refetch every (or each selected) substance into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(binding.options.get("languages", LANGUAGES))
    if set(configured) - set(LANGUAGES):
        raise ValueError(f"SMHI emissions publishes Swedish only, not {configured}")
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured):
        raise ValueError(f"Unconfigured SMHI emissions languages: {selected_languages}")
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not _allowed_path(relative) for relative in baseline):
        raise ValueError("Unexpected file in published SMHI emissions data")
    state = load_state(data)
    output.mkdir(parents=True, exist_ok=True)
    for relative in baseline:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(data / relative, target)
    store = SourceStore(output / "source", ADAPTER)
    # Placeholders stand in for documents: presence means a stored source entry.
    published, withheld = {}, {}
    for stored in store.entries():
        key = state_key(stored)
        status = state["datasets"].get(key, {}).get("status")
        (withheld if status == "withheld" else published)[key] = True
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    max_age = timedelta(days=binding.options.get("max_age_days", 60))
    cooldown = binding.options.get("retry_cooldown_seconds", 60)
    logger.info(
        "Starting %s full rebuild: subjects=%s languages=%s max_age_days=%s "
        "retry_cooldown=%ss; the adapter has no refresh mode, every selected "
        "substance is re-probed",
        ADAPTER,
        subjects if subjects is not None else "all",
        selected_languages,
        max_age.days,
        cooldown,
    )
    logger.info(
        "Baseline: %d published entries, %d withheld, %d state entries, %d files "
        "copied to the worker output",
        len(published),
        len(withheld),
        len(state["datasets"]),
        len(baseline),
    )
    if state.get("mapping_version") != MAPPING_VERSION:
        logger.info(
            "Stored mapping version %s differs from current %s; documents are "
            "rebuilt with the new mapping",
            state.get("mapping_version"),
            MAPPING_VERSION,
        )
    connection = client or SmhiClient()
    progress = Progress(ADAPTER, PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors, submissions = [], {}

    def fail(
        stage,
        resource,
        error,
        url=FILTER_OPTIONS_URL,
        *,
        record=True,
        emit=True,
        **details,
    ):
        entry = {
            "stage": stage,
            "resource": resource,
            "url": url,
            "error": f"{type(error).__name__}: {error}",
        }
        if emit:
            logger.error(
                "%s failure for %s%s: %s",
                stage,
                resource,
                " (recorded)" if record else "",
                entry["error"],
            )
        if record:
            errors.append(entry)
        if emit:
            progress.error(stage, resource, "sv", entry["error"], url=url, **details)
        return entry

    def known():
        keys = set(published) | set(withheld) | set(state["datasets"])
        return {key for key in keys if key.split("/", 1)[0] in selected_languages}

    try:
        progress.update(phase="discovery", current="filteroptions")
        try:
            tree = await connection.get_json(FILTER_OPTIONS_URL)
            options = parse_filter_options(tree)
            if not options.substances and not options.quarantined:
                raise ValueError("getfilteroptions lists no substances")
        except Exception as error:
            fail("discovery", "filteroptions", error, record=False)
            logger.error("Discovery failed; the run publishes nothing")
            raise
        store.put_shared(PROVIDER_CODE, "filteroptions.json", tree, retrieved_at=stamp)
        logger.info(
            "Discovered %d sectors and %d substances; %d quarantined, %d malformed "
            "entries; tree complete=%s",
            len(options.sectors),
            len(options.substances),
            len(options.quarantined),
            len(options.problems),
            options.complete,
        )
        for code, reason in options.quarantined.items():
            logger.warning("Quarantined substance %s: %s", code, reason)
        for resource, error in options.problems:
            fail("discovery", resource, error)
        selected, unknown = _select(options, subjects)
        for name in unknown:
            fail("discovery", f"*/{name}", LookupError("Unknown SMHI substance"))

        listed = {
            f"{language}/{dataset_code(code)}"
            for code in [*options.substances, *options.quarantined]
            for language in selected_languages
        }
        if subjects is None and options.complete:
            gone = sorted(known() - listed)
            logger.info(
                "Complete tree in an unbounded run: %d previously known datasets "
                "are no longer listed and are removed",
                len(gone),
            )
            for key in gone:
                state["datasets"].pop(key, None)
                store.remove(stored_key(key))
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1
                    logger.info("Removed %s: substance no longer listed", key)
        else:
            logger.info(
                "Removal check skipped (%s): absent datasets are kept",
                "subject-filtered run" if subjects is not None else "incomplete tree",
            )

        working, failures, settled = {}, {}, set()
        pending = []
        for code in selected:
            for language in selected_languages:
                key = f"{language}/{dataset_code(code)}"
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                if code in options.quarantined:  # keep the last document until stale
                    logger.info(
                        "Not probing %s: quarantined, last document kept until stale",
                        key,
                    )
                    working[key] = True
                    failures[key] = ("metadata", ValueError(options.quarantined[code]))
                    fail(
                        "metadata", key, failures[key][1], record=False, retrying=False
                    )
                else:
                    pending.append((key, options.substances[code]))

        logger.info(
            "Probing %d of %d candidate datasets (%d quarantined skipped); every "
            "selected substance is refetched, nothing is skipped as unchanged",
            len(pending),
            len(settled),
            len(settled) - len(pending),
        )
        progress.update(phase="metadata", total=len(pending), processed=0)
        for attempt in (1, 2):
            logger.info("Rebuild pass %d: %d datasets", attempt, len(pending))
            retry = []
            for key, substance in pending:
                progress.update(current=key, operation=f"rebuild (attempt {attempt})")
                info = state["datasets"][key]
                url = f"{DATA_URL}?{urlencode(probe_parameters(substance.code))}"
                try:
                    body = await probe_substance(connection, substance)
                except Unreachable as error:
                    logger.warning(
                        "Probe of %s unreachable on attempt %d; %s",
                        key,
                        attempt,
                        "will retry after the cooldown"
                        if attempt == 1
                        else "giving up",
                    )
                    working[key], failures[key] = False, ("endpoint", error)
                    fail(
                        "endpoint",
                        key,
                        error,
                        url,
                        record=False,
                        attempt=attempt,
                        retrying=attempt == 1,
                    )
                    progress.update(failed=len(failures))
                    retry.append((key, substance))
                    continue
                working[key] = True  # the probe answered: endpoint works
                submission = submission_of(body)
                stored = store.get(stored_key(key))
                try:
                    store.put(
                        stored_key(key),
                        responses={"getdata": body},
                        derived={"substance": asdict(substance)},
                        updated=submission,
                        retrieved_at=stamp,
                    )
                except Rejected as error:
                    logger.warning(
                        "Probe of %s answered but is not storable (%s); not retried",
                        key,
                        error,
                    )
                    failures[key] = ("metadata", error)
                    fail(
                        "metadata",
                        key,
                        error,
                        url,
                        record=False,
                        attempt=attempt,
                        retrying=False,
                    )
                    progress.update(failed=len(failures))
                    continue
                failures.pop(key, None)
                submissions[substance.code] = submission
                unchanged = stored is not None and stored["responses"] == {
                    "getdata": body
                }
                outcome = "unchanged" if unchanged else "rebuilt"
                counts[outcome] += 1
                logger.debug("Stored %s: %s (submission %s)", key, outcome, submission)
                info.update(
                    verified_at=stamp,
                    mapping_version=MAPPING_VERSION,
                    submission=submission,
                    error=None,
                )
                (withheld if key in withheld else published)[key] = True
                progress.update(
                    processed=counts["rebuilt"] + counts["unchanged"],
                    succeeded=counts["rebuilt"] + counts["unchanged"],
                    failed=len(failures),
                )
            logger.info(
                "Pass %d done: %d rebuilt, %d unchanged so far, %d to retry",
                attempt,
                counts["rebuilt"],
                counts["unchanged"],
                len(retry),
            )
            if not retry or attempt == 2:
                break
            logger.info(
                "Retrying %d unreachable datasets after a %ss cooldown",
                len(retry),
                cooldown,
            )
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            pending = retry
        for key, (stage, error) in sorted(failures.items()):
            code = key.split("/", 1)[1].removeprefix(DATASET_PREFIX)
            url = (
                FILTER_OPTIONS_URL
                if code in options.quarantined
                else f"{DATA_URL}?{urlencode(probe_parameters(code))}"
            )
            state["datasets"][key]["error"] = fail(stage, key, error, url, emit=False)
            counts["failed"] += 1
        progress.update(failed=counts["failed"])
        logger.info(
            "Settling %d datasets: published only when the probe answered and the "
            "entry was verified within %d days",
            len(settled),
            max_age.days,
        )
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
        for key in sorted(settled):
            info = state["datasets"][key]
            if info.get("status") == "withheld":
                logger.warning(
                    "Withheld %s: %s (last verified %s)",
                    key,
                    info.get("withheld_reason"),
                    info.get("verified_at"),
                )
            elif info.get("status") == "missing":
                logger.warning("No stored source entry exists for %s", key)
            else:
                logger.debug("Settled %s as %s", key, info.get("status"))
    finally:
        if client is None:
            await connection.close()

    state["mapping_version"] = MAPPING_VERSION
    store.save()
    save(output, state)
    status = report_status(pending=0, errors=errors)
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "selection": {"subjects": subjects, "languages": selected_languages},
        "counts": counts,
        "published": {
            language: sum(k.startswith(f"{language}/") for k in published)
            for language in configured
        },
        "withheld": sorted(withheld),
        "submissions": sorted({s for s in submissions.values() if s is not None}),
        "tree_complete": options.complete,
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    logger.info(
        "Finished with status %s: %s; published=%s withheld=%d errors=%d "
        "submissions=%s tree_complete=%s",
        status,
        ", ".join(f"{name}={counts[name]}" for name in COUNTS),
        result["published"],
        len(withheld),
        len(errors),
        result["submissions"],
        options.complete,
    )
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting worker output (run %s, status %s, %d files)",
        result.get("run_id"),
        result.get("status"),
        len(result.get("manifest") or ()),
    )
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        logger.error("Published baseline changed since the run; refusing to collect")
        raise ValueError("Stale SMHI emissions artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("SMHI emissions artifact checksum mismatch")
    if not {STATE, REPORT} <= set(result["manifest"]) or not all(
        _allowed_path(relative) for relative in result["manifest"]
    ):
        raise ValueError("Unexpected SMHI emissions artifact path")
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
    logger.info("Applied %d files to the published data", len(result["manifest"]))
    return result

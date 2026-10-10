"""Weekly Riksbank refresh with independent API, row and dataset failures.

The Riksbank publishes no ETag, Last-Modified or metadata timestamp, and a full
run is under 60 requests, so every run rebuilds every dataset; an identical
rebuild keeps its accepted bytes. Discovery reads four catalogue responses: SWEA
``/Groups`` and ``/Series``, and the forecast API's ``series_ids`` and
``policy_rounds``. Each dataset then makes one small probe, which is its endpoint
check: the group's latest observations, the series' latest-round vintage, or the
latest SWESTR fixing (the SWESTR probe is also its discovery). Requests are paced
at ``request_interval_seconds`` (15 s), so a full run takes about 12 minutes.

The three APIs (``swea``, ``mpd``, ``swestr``) are independent discovery scopes: a
failed scope is reported with that resource name and keeps its datasets, while the
others continue; only when every attempted scope fails does the run raise and
publish nothing. A dataset is published only while its endpoint works and its
document was verified within ``max_age_days``; otherwise its last good document is
withheld in state. Only a complete discovery in an unbounded run proves removal.
"""

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.riksbank.api import (
    GROUPS_URL,
    MPD_FORECASTS,
    MPD_POLICY_ROUNDS,
    MPD_SERIES_IDS,
    SERIES_URL,
    SWESTR_LATEST_URL,
    latest_by_group_url,
)
from apps.harvest.metadata_adapters.riksbank.client import RiksbankClient, Unreachable
from apps.harvest.metadata_adapters.riksbank.mapping import (
    MAPPING_VERSION,
    build_mpd,
    build_swea_group,
    build_swestr,
)
from apps.harvest.metadata_adapters.riksbank.source import (
    Listing,
    mpd_listing,
    swea_listing,
    swestr_listing,
)
from apps.harvest.metadata_adapters.riksbank.state import (
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
from retrieval.data_adapters.riksbank import api_of

LANGUAGES = ("en",)
APIS = ("swea", "mpd", "swestr")
logger = logging.getLogger("riksbank_logger")

PROGRESS_EVERY = 10
"""Log a progress line after this many built datasets."""
COUNTS = ("rebuilt", "unchanged", "failed", "withheld", "restored", "removed")


async def _request(client, url, params=None, **kwargs):
    """Preserve the failed catalogue request's URL for live telemetry."""
    try:
        return await client.get_json(url, params, **kwargs)
    except Exception as error:
        error.source_url = f"{url}?{urlencode(params)}" if params else url
        raise


def _probe_url(record):
    if record["api"] == "swea":
        return latest_by_group_url(record["group"]["groupId"])
    if record["api"] == "mpd":
        params = {
            "series": record["series"]["series_id"],
            "policy_round_name": record["policy_rounds"][-1],
        }
        return f"{MPD_FORECASTS}?{urlencode(params)}"
    return SWESTR_LATEST_URL


async def discover_api(client, api: str) -> Listing:
    """Read one API's catalogue; raises when the whole scope cannot be read."""
    logger.debug("Reading the %s catalogue", api)
    if api == "swea":
        groups = await _request(client, GROUPS_URL)
        return swea_listing(groups, await _request(client, SERIES_URL))
    if api == "mpd":
        series = await _request(client, MPD_SERIES_IDS)
        return mpd_listing(series, await _request(client, MPD_POLICY_ROUNDS))
    return swestr_listing(await _request(client, SWESTR_LATEST_URL))


async def build_record(client, record: dict) -> dict:
    """Probe one dataset's endpoint, then build and validate its document.

    Raises:
        Unreachable: The probe failed (an endpoint failure).
        Exception: Any other error is a metadata failure.
    """
    if record["api"] == "swea":
        group_id = record["group"]["groupId"]
        logger.debug("Probing SWEA group %s for its latest observations", group_id)
        latest = await client.get_json(latest_by_group_url(group_id))
        if latest is None:
            raise ValueError(f"no latest observations for SWEA group {group_id}")
        document = build_swea_group(record, latest)
    elif record["api"] == "mpd":
        series_id = record["series"]["series_id"]
        latest_round = record["policy_rounds"][-1]
        logger.debug(
            "Probing forecast %s at its latest round %s", series_id, latest_round
        )
        probe = await client.get_json(
            MPD_FORECASTS,
            {"series": series_id, "policy_round_name": latest_round},
            missing_ok=True,
        )
        if probe is None:
            raise ValueError(f"no {latest_round} vintage of {series_id}")
        document = build_mpd(record, probe)
    else:  # discovery already read the latest fixing
        logger.debug("SWESTR needs no probe: discovery already read the latest fixing")
        document = build_swestr(record["latest"])
    validate_metadata(document, adapter=PROVIDER_CODE)
    logger.debug("Validated the %s document", record["dataset_code"])
    return document


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """Every dataset record of the three APIs (four catalogue requests)."""
    connection = client or RiksbankClient(provider)
    try:
        records = []
        for api in APIS:
            found = (await discover_api(connection, api)).records
            logger.info("Discovered %d %s dataset records", len(found), api)
            records += found
        logger.info("Discovered %d Riksbank dataset records in total", len(records))
        return records
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> dict:
    """Build and validate the document of one discovered record."""
    logger.debug("Fetching metadata of %s", record["dataset_code"])
    return await build_record(client, record)


def _apis_for(names) -> list[str]:
    """The APIs whose catalogues can resolve the selected names."""
    if names is None:
        return list(APIS)
    found = set()
    for name in names:
        folded = name.casefold()
        if api := api_of(folded):
            found.add(api)
        elif folded.isdigit():
            found.add("swea")
        elif folded == "swestr":
            found.add("swestr")
        else:  # a SWEA or forecast series id
            found.update(("swea", "mpd"))
    return [api for api in APIS if api in found]


def _aliases(record: dict) -> set[str]:
    """Casefolded names selecting a record: its code and native ids."""
    names = {record["dataset_code"].casefold()}
    if record["api"] == "swea":
        names.add(str(record["group"]["groupId"]))
        names.update(row["seriesId"].casefold() for row in record["series"])
    elif record["api"] == "mpd":
        names.add(record["series"]["series_id"].casefold())
    return names


async def run(
    provider: dict,
    data: Path,
    output: Path,
    *,
    client=None,
    clock=None,
    subjects=None,
    languages=None,
) -> dict:
    """Rebuild every (or each selected) dataset into a separate worker output.

    Args:
        subjects: Optional dataset codes (``swea_group_2``, ``mpd_SEQGDPNAYSA``,
            ``swestr``), SWEA group ids, or SWEA/forecast series ids, matched
            case-insensitively; a bounded run touches only those datasets.
        languages: Optional subset of the configured languages (``en``).
    """
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured) or set(configured) - set(LANGUAGES):
        raise ValueError(f"Unconfigured Riksbank languages: {selected_languages}")
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published Riksbank data")
    state = load_state(data)
    published = load_documents(data, configured)
    withheld = state["withheld"]
    logger.info(
        "Loaded published baseline from %s: %d files, %d published documents, "
        "%d withheld, state for %d datasets (state mapping version %s, current %s)",
        data,
        len(baseline),
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
    connection = client or RiksbankClient(provider)
    progress = Progress(PROVIDER_CODE, PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors: list[dict] = []
    wanted = {name.casefold() for name in subjects} if subjects else None
    apis = _apis_for(subjects)
    logger.info(
        "Riksbank %s: languages %s; APIs to discover %s of %s; max age %d days, "
        "retry cooldown %ss, request interval %ss",
        "full rebuild" if wanted is None else "bounded rebuild",
        selected_languages,
        apis,
        list(APIS),
        max_age.days,
        cooldown,
        getattr(connection, "_interval", None),
    )
    if wanted is None:
        logger.info(
            "No ETag, Last-Modified or timestamp is published, so every discovered "
            "dataset is rebuilt and probed; an identical rebuild keeps its bytes"
        )
    else:
        logger.info(
            "Only datasets matching %d selected names %s are rebuilt; the names "
            "narrow discovery to %d of %d APIs and resolve against SWEA group "
            "ids/series ids, forecast series ids and dataset codes",
            len(wanted),
            sorted(subjects),
            len(apis),
            len(APIS),
        )
    scopes: dict[str, str] = {}
    matched: set[str] = set()
    failure_urls = {}

    def notify(stage, resource, error, url):
        failure_urls[(stage, resource)] = url
        progress.error(
            stage, resource, LANGUAGES[0], f"{type(error).__name__}: {error}", url=url
        )

    def fail(stage, resource, error, *, url=None, report_only=False):
        url = (
            url
            or failure_urls.get((stage, resource))
            or (
                SERIES_URL
                if resource.startswith(("swea/series", "en/swea_"))
                else GROUPS_URL
                if resource.startswith("swea/")
                else MPD_SERIES_IDS
                if resource.startswith(("mpd/", "en/mpd_"))
                else SWESTR_LATEST_URL
            )
        )
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        logger.log(
            logging.DEBUG if report_only else logging.ERROR,
            "Recorded %s failure for %s: %s: %s",
            stage,
            resource,
            type(error).__name__,
            error,
        )
        if not report_only:
            notify(stage, resource, error, url)
        return entry

    def known(api: str) -> set[str]:
        """Accepted keys of ``api`` in the selected languages and selection."""
        keys = set(published) | set(withheld) | set(state["datasets"])
        found = set()
        for key in keys:
            language, code = key.split("/", 1)
            if language not in selected_languages or api_of(code) != api:
                continue
            if wanted is not None and not _known_selected(key, code):
                continue
            found.add(key)
        return found

    def _known_selected(key: str, code: str) -> bool:
        if code.casefold() in wanted:
            return True
        if code.startswith("swea_group_") and code[11:] in wanted:
            return True
        accepted = published.get(key) or withheld.get(key)
        if accepted is None:
            return code.startswith("mpd_") and code[4:].casefold() in wanted
        categories = accepted.get("dimension", {}).get("Series", {}).get("category", {})
        return any(
            series.casefold() in wanted for series in categories.get("index", [])
        )

    working: dict[str, bool] = {}
    failures: dict[str, tuple[str, Exception]] = {}
    settled: set[str] = set()

    def keys_of(record: dict) -> list[str]:
        return [
            f"{language}/{record['dataset_code']}" for language in selected_languages
        ]

    async def build(record: dict) -> bool:
        """Rebuild one dataset in every language; True on an endpoint failure."""
        endpoint_failed = False
        document = None
        try:
            document = await build_record(connection, record)
            outcome = None
        except Unreachable as error:
            outcome, endpoint_failed = ("endpoint", error), True
            logger.warning(
                "%s: probe endpoint unreachable (%s); queued for one retry after the "
                "cooldown, and the last good document is withheld if it stays down",
                record["dataset_code"],
                error,
            )
        except Exception as error:  # the probe answered: endpoint works
            outcome = ("metadata", error)
            logger.error(
                "%s: probe answered but the document could not be built (%s: %s); "
                "the last good document stays published while it is still fresh",
                record["dataset_code"],
                type(error).__name__,
                error,
            )
        for key in keys_of(record):
            settled.add(key)
            info = state["datasets"].setdefault(key, {})
            info["checked_at"] = stamp
            if outcome is not None:
                working[key] = outcome[0] == "metadata"
                failures[key] = outcome
                notify(outcome[0], key, outcome[1], _probe_url(record))
                continue
            failures.pop(key, None)
            working[key] = True
            accepted = published.get(key) or withheld.get(key)
            counts["unchanged" if document == accepted else "rebuilt"] += 1
            logger.debug(
                "%s: %s",
                key,
                "identical to the accepted document, keeping it"
                if document == accepted
                else "new document"
                if accepted is None
                else "document changed, replacing the accepted one",
            )
            info.update(verified_at=stamp, mapping_version=MAPPING_VERSION, error=None)
            (withheld if key in withheld else published)[key] = document
        processed = counts["rebuilt"] + counts["unchanged"] + len(failures)
        progress.update(
            processed=processed,
            succeeded=counts["rebuilt"] + counts["unchanged"],
        )
        if processed and processed % PROGRESS_EVERY == 0:
            logger.info(
                "Built %d datasets so far: %d rebuilt, %d unchanged, %d failing",
                processed,
                counts["rebuilt"],
                counts["unchanged"],
                len(failures),
            )
        return endpoint_failed

    async def harvest(api: str) -> tuple[bool, list[dict]]:
        """Discover one API; return (discovery endpoint failed, records to build)."""
        progress.update(phase="discovery", current=api)
        try:
            listing = await discover_api(connection, api)
        except Exception as error:
            logger.error(
                "Discovery of the %s API failed (%s: %s); %s",
                api,
                type(error).__name__,
                error,
                "endpoint unreachable, will retry after the cooldown"
                if isinstance(error, Unreachable)
                else "catalogue unusable, no retry",
            )
            notify(
                "discovery",
                api,
                error,
                getattr(
                    error,
                    "source_url",
                    {
                        "swea": SERIES_URL,
                        "mpd": MPD_SERIES_IDS,
                        "swestr": SWESTR_LATEST_URL,
                    }[api],
                ),
            )
            unavailable = known(api)
            logger.warning(
                "%s scope failed: not rebuilding its %d known datasets; each is "
                "withheld unless it was verified within the maximum age and its "
                "endpoint still works",
                api,
                len(unavailable),
            )
            for key in unavailable:
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                working[key] = not isinstance(error, Unreachable)
            failures[api] = ("discovery", error)
            scopes[api] = "failed"
            return isinstance(error, Unreachable), []
        failures.pop(api, None)
        scopes[api] = "complete" if listing.complete else "incomplete"
        for resource, error in listing.problems:
            fail("discovery", resource, error)
        records = listing.records
        logger.info(
            "Discovery %s: %s, %d dataset records, %d catalogue problems, "
            "%d quarantined datasets (%s)",
            api,
            scopes[api],
            len(records),
            len(listing.problems),
            len(listing.quarantined),
            ", ".join(sorted(listing.quarantined)) or "none",
        )
        if wanted is not None:
            records = [r for r in records if _aliases(r) & wanted]
            for record in listing.records:
                matched.update(_aliases(record) & wanted)
            for code in listing.quarantined:
                matched.add(code.casefold())
            logger.info(
                "Subject filter on %s: selected %d of %d records, skipped %d not "
                "matching %s; no removal check in a bounded run",
                api,
                len(records),
                len(listing.records),
                len(listing.records) - len(records),
                sorted(subjects),
            )
        elif listing.complete:
            current = {key for record in records for key in keys_of(record)}
            previous = known(api)
            gone = sorted(previous - current)
            logger.info(
                "Removal check on %s: complete listing, %d of %d known keys are no "
                "longer listed",
                api,
                len(gone),
                len(previous),
            )
            for key in gone:
                settled.discard(key)
                state["datasets"].pop(key, None)
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1
                logger.warning(
                    "%s is no longer listed by the %s API; removed", key, api
                )
        else:
            logger.info(
                "Removal check on %s skipped: the listing is incomplete, so absence "
                "proves nothing",
                api,
            )
        logger.debug(
            "%s datasets to build: %s", api, [r["dataset_code"] for r in records]
        )
        return False, records

    try:
        progress.update(phase="discovery", current=None, apis=apis)
        pending: list[dict] = []
        retry_apis = []
        for api in apis:
            unreachable, records = await harvest(api)
            if unreachable:
                retry_apis.append(api)
            pending += records
        progress.update(phase="metadata", total=len(pending), processed=0)
        logger.info(
            "Discovery done: %d datasets selected for rebuild; %d API scopes need a "
            "retry; expecting %d probe requests",
            len(pending),
            len(retry_apis),
            sum(r["api"] != "swestr" for r in pending),
        )
        retry_records = []
        for record in pending:
            progress.update(current=record["dataset_code"], operation="rebuild")
            if await build(record):
                retry_records.append(record)
        logger.info(
            "First pass done: %d rebuilt, %d unchanged, %d failing; %d datasets and "
            "%d API scopes to retry",
            counts["rebuilt"],
            counts["unchanged"],
            len(failures),
            len(retry_records),
            len(retry_apis),
        )
        if retry_apis or retry_records:
            progress.update(operation="retry cooldown", current=None)
            logger.info("Waiting %ss before retrying unreachable endpoints", cooldown)
            await asyncio.sleep(cooldown)
            for api in retry_apis:
                logger.info("Retrying discovery of the %s API", api)
                _, records = await harvest(api)
                retry_records += records
            for record in retry_records:
                progress.update(current=record["dataset_code"], operation="retry")
                await build(record)
        if apis and all(scopes.get(api) == "failed" for api in apis):
            logger.error(
                "Every attempted API scope failed (%s); publishing nothing",
                ", ".join(apis),
            )
            raise failures[apis[0]][1]
        if wanted is not None:
            resolvable = all(scopes.get(api) != "failed" for api in apis)
            if not resolvable:
                logger.warning(
                    "Cannot tell whether selected names are unknown: an API scope "
                    "failed"
                )
            for name in sorted(subjects) if resolvable else []:
                if name.casefold() not in matched:
                    fail("discovery", name, LookupError("Unknown Riksbank dataset"))
        for key, (stage, error) in sorted(failures.items()):
            entry = fail(stage, key, error, report_only=True)
            counts["failed"] += 1
            for affected in known(key) if key in APIS else [key]:
                if affected in state["datasets"]:
                    state["datasets"][affected]["error"] = entry
        progress.update(failed=counts["failed"])
        logger.info(
            "Settling %d keys: published only if the endpoint worked this run and "
            "the document was verified within %d days, otherwise withheld",
            len(settled),
            max_age.days,
        )
        was_withheld = set(withheld)
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
        for key in sorted(set(withheld) - was_withheld):
            logger.warning(
                "%s withheld: %s", key, state["datasets"][key].get("withheld_reason")
            )
        for key in sorted(was_withheld - set(withheld)):
            logger.info("%s restored to the published set", key)
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
        "selection": {
            "subjects": sorted(subjects) if subjects else None,
            "languages": selected_languages,
        },
        "apis": {api: scopes.get(api, "not attempted") for api in apis},
        "requests": getattr(connection, "request_count", None),
        "counts": counts,
        "published": {
            language: sum(k.startswith(f"{language}/") for k in published)
            for language in configured
        },
        "withheld": sorted(withheld),
        "errors": errors,
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=baseline, manifest=manifest(output))
    write(output / "result.json", result)
    progress.update(phase="finished", result_status=status, current=None)
    logger.log(
        logging.INFO if not errors else logging.WARNING,
        "Riksbank run finished with status %s after %s requests: counts %s; "
        "published %s; %d withheld; %d errors; scopes %s",
        status,
        result["requests"],
        counts,
        result["published"],
        len(withheld),
        len(errors),
        result["apis"],
    )
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting Riksbank artifact %s (status %s, %d files) into %s",
        output,
        result.get("status"),
        len(result["manifest"]),
        data,
    )
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale Riksbank artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Riksbank artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        raise ValueError("Unexpected Riksbank artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
        logger.debug("Applied %s", relative)
    logger.info("Collected %d files into %s", len(result["manifest"]), data)
    return result

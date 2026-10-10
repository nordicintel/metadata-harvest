"""Weekly SDB refresh with independent subject and language failures.

SDB publishes no modification dates and a full harvest is about 200 requests, so
every run rebuilds every subject in every language; an identical rebuild keeps its
accepted bytes. The result probe's first-row request is each subject's endpoint
check. A subject is published only while that endpoint works and its document was
verified within ``max_age_days``; otherwise its last good document is withheld in
state and restored automatically when both hold again. Only a complete listing in
an unbounded run proves a subject's removal from that language.
"""

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.sdb.client import (
    NotFoundError,
    SdbClient,
    Unreachable,
)
from apps.harvest.metadata_adapters.sdb.mapping import (
    MAPPING_VERSION,
    Subject,
    build_dataset,
    time_unit_from_swedish_label,
)
from apps.harvest.metadata_adapters.sdb.probe import probe_results
from apps.harvest.metadata_adapters.sdb.state import (
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

logger = logging.getLogger("sdb_logger")

LANGUAGES = ("sv", "en")
PROGRESS_EVERY = 25
LISTED_LIMIT = 20
COUNTS = ("rebuilt", "unchanged", "failed", "withheld", "restored", "removed")


def _listed(keys) -> str:
    """Comma-joined sorted ``keys``, truncated so one log line stays readable."""
    keys = sorted(keys)
    shown = ", ".join(keys[:LISTED_LIMIT])
    if len(keys) > LISTED_LIMIT:
        shown += f", ... (+{len(keys) - LISTED_LIMIT} more)"
    return shown


def listing_rows(body, problems: list) -> tuple[list[dict], bool]:
    """Valid, unique subject rows; malformed or duplicate rows make it incomplete."""
    if not isinstance(body, list):
        raise ValueError(f"unexpected SDB subject listing {str(body)[:200]!r}")
    complete, found = True, {}
    for row in body:
        if not (
            isinstance(row, dict)
            and isinstance(row.get("namn"), str)
            and row["namn"]
            and isinstance(row.get("text"), str)
        ):
            problems.append((str(row)[:200], ValueError("Malformed subject row")))
            complete = False
            continue
        found.setdefault(row["namn"], []).append(row)
    rows = []
    for name, matches in found.items():
        if len(matches) > 1:
            problems.append((name, ValueError("Listed more than once")))
            complete = False
        else:
            rows.append(matches[0])
    return rows, complete


async def build_subject(client, row: dict, language: str, time_unit, probes: dict):
    """Fetch, probe, build and validate one subject's document in ``language``."""
    code = row["namn"]
    try:
        variables = await client.get(language, code)
        categories = {
            str(v["namn"]): await client.get(language, code, str(v["namn"]))
            for v in variables
        }
        logger.debug(
            "%s/%s: fetched %d variables and %d category listings",
            language,
            code,
            len(variables),
            len(categories),
        )
        if code in probes:  # probed in another language; check this endpoint too
            logger.debug(
                "%s/%s: reusing the result probe from another language; checking "
                "only this language's result endpoint",
                language,
                code,
            )
            await client.get(language, code, "resultat", params={"per_sida": 1})
        else:
            probes[code] = await probe_results(
                client, language, code, variables, categories
            )
    except NotFoundError as error:
        logger.warning("%s/%s: SDB answered 404: %s", language, code, error)
        raise Unreachable(str(error)) from error
    probe = probes[code]
    document = build_dataset(
        Subject(code, row["text"].strip(), time_unit),
        language,
        variables,
        categories,
        result_dimensions=probe.result_dimensions,
        added_periods=probe.added_periods,
        source_record=row,
    )
    validate_metadata(document, adapter="sdb")
    logger.debug("%s/%s: document built and validated", language, code)
    return document


async def discover_provider(provider: dict, *, client=None) -> dict:
    """Each configured language's subject listing."""
    connection = client or SdbClient(provider)
    try:
        listings = {
            language: await connection.get(language)
            for language in provider.get("languages", LANGUAGES)
        }
        logger.info(
            "Discovered SDB subject listings: %s",
            ", ".join(f"{k}={len(v)} rows" for k, v in listings.items()),
        )
        return listings
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> dict:
    """Build one document from ``{"language", "namn", "text", "time_unit"}``."""
    row = {"namn": record["namn"], "text": record["text"]}
    logger.debug("Fetching metadata for %s/%s", record["language"], record["namn"])
    return await build_subject(
        client, row, record["language"], record.get("time_unit"), {}
    )


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
    """Rebuild every (or each selected) subject into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured):
        raise ValueError(f"Unconfigured SDB languages: {selected_languages}")
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published SDB data")
    state = load_state(data)
    published = load_documents(data, configured)
    withheld = state["withheld"]
    logger.info(
        "Baseline: %d published documents, %d withheld, %d tracked subjects "
        "across %d files",
        len(published),
        len(withheld),
        len(state["subjects"]),
        len(baseline),
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
    connection = client or SdbClient(provider)
    progress = Progress("sdb", PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors = []
    wanted = set(subjects) if subjects else None
    logger.info(
        "SDB run starting: %s; languages=%s (configured %s); subjects=%s; "
        "max_age_days=%s; retry_cooldown_seconds=%s; SDB publishes no modification "
        "dates, so every selected subject is rebuilt and compared with its accepted "
        "document",
        "filtered rebuild" if wanted else "full rebuild",
        selected_languages,
        configured,
        sorted(wanted) if wanted else "all",
        max_age.days,
        cooldown,
    )
    if state.get("mapping_version") != MAPPING_VERSION:
        logger.info(
            "Mapping version changed (%s -> %s); rebuilt documents may differ from "
            "accepted ones",
            state.get("mapping_version"),
            MAPPING_VERSION,
        )

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        logger.error("Recorded %s failure for %s: %s", stage, resource, entry["error"])
        progress.error(stage, resource, resource.split("/", 1)[0], entry["error"])
        return entry

    def known(language):
        prefix = f"{language}/"
        keys = set(published) | set(withheld) | set(state["subjects"])
        return {key for key in keys if key.startswith(prefix)}

    try:
        progress.update(phase="discovery", current="sv")
        problems = []
        swedish, sv_complete = listing_rows(await connection.get("sv"), problems)
        for resource, error in problems:
            fail("discovery", f"sv/{resource}", error)
        logger.info(
            "Swedish listing: %d valid subjects, %d malformed or duplicate rows, "
            "listing %s",
            len(swedish),
            len(problems),
            "complete" if sv_complete else "incomplete",
        )
        if not swedish:
            logger.error("SDB lists no Swedish subjects; aborting the run")
            raise ValueError("SDB lists no Swedish subjects")
        time_units = {
            row["namn"]: time_unit_from_swedish_label(row["text"]) for row in swedish
        }
        logger.debug(
            "Time units from Swedish labels: %s", dict(sorted(time_units.items()))
        )
        for code in sorted((wanted or set()) - set(time_units)):
            logger.warning("Requested subject %s is not in the Swedish listing", code)
            fail("discovery", f"*/{code}", LookupError("Unknown SDB subject"))

        tasks, working, settled = [], {}, set()
        for language in selected_languages:
            if language == "sv":
                rows, complete = swedish, sv_complete
            else:
                problems = []
                try:
                    progress.update(current=language)
                    rows, complete = listing_rows(
                        await connection.get(language), problems
                    )
                except (Unreachable, NotFoundError, ValueError) as error:
                    fail("discovery", language, error)
                    unavailable = known(language)
                    if wanted is not None:
                        unavailable = {
                            k for k in unavailable if k.split("/", 1)[1] in wanted
                        }
                    logger.warning(
                        "%s listing unavailable; skipping the language and marking "
                        "its %d known subjects as not working (no removals)",
                        language,
                        len(unavailable),
                    )
                    for key in unavailable:
                        state["subjects"].setdefault(key, {})["checked_at"] = stamp
                        working[key] = False
                    settled |= unavailable
                    continue
                for resource, error in problems:
                    fail("discovery", f"{language}/{resource}", error)
            listed = {f"{language}/{row['namn']}" for row in rows}
            logger.info(
                "%s listing: %d subjects, %d known from earlier runs, %s",
                language,
                len(listed),
                len(known(language)),
                "complete" if complete else "incomplete",
            )
            if complete and wanted is None:
                gone = sorted(known(language) - listed)
                for key in gone:
                    state["subjects"].pop(key, None)
                    if (
                        published.pop(key, None) or withheld.pop(key, None)
                    ) is not None:
                        counts["removed"] += 1
                if gone:
                    logger.warning(
                        "%s: %d known subjects are no longer listed and are removed "
                        "(complete, unbounded listing): %s",
                        language,
                        len(gone),
                        _listed(gone),
                    )
            else:
                logger.info(
                    "%s: removal check skipped (%s); known subjects missing from "
                    "the listing are kept",
                    language,
                    "subject filter active"
                    if wanted is not None
                    else "listing incomplete",
                )
            selected = [
                (language, row)
                for row in rows
                if wanted is None or row["namn"] in wanted
            ]
            tasks += selected
            logger.info(
                "%s: selected %d of %d listed subjects for rebuild%s",
                language,
                len(selected),
                len(rows),
                "" if wanted is None else " (subject filter)",
            )
            if len(selected) < len(rows):
                chosen = {row["namn"] for _, row in selected}
                logger.debug(
                    "%s: skipped by subject filter: %s",
                    language,
                    _listed(row["namn"] for row in rows if row["namn"] not in chosen),
                )

        logger.info(
            "Rebuild plan: %d subject documents across %s",
            len(tasks),
            selected_languages,
        )

        progress.update(phase="metadata", total=len(tasks), processed=0)
        probes, failures, pending = {}, {}, tasks
        for attempt in (1, 2):
            retry = []
            logger.info(
                "Attempt %d/2: rebuilding %d subject documents%s",
                attempt,
                len(pending),
                " (endpoint failures from attempt 1)" if attempt == 2 else "",
            )
            for position, (language, row) in enumerate(pending, 1):
                key = f"{language}/{row['namn']}"
                settled.add(key)
                progress.update(current=key, operation=f"rebuild (attempt {attempt})")
                info = state["subjects"].setdefault(key, {})
                info["checked_at"] = stamp
                try:
                    document = await build_subject(
                        connection, row, language, time_units.get(row["namn"]), probes
                    )
                except Unreachable as error:
                    working[key] = False
                    failures[key] = ("endpoint", error)
                    retry.append((language, row))
                    logger.warning(
                        "%s: endpoint failure on attempt %d/2 (%s); %s",
                        key,
                        attempt,
                        error,
                        "queued for retry after cooldown"
                        if attempt == 1
                        else "giving up for this run",
                    )
                    continue
                except Exception as error:
                    failures[key] = ("metadata", error)
                    try:  # a mapping failure keeps a working endpoint published
                        await connection.get(
                            language, row["namn"], "resultat", params={"per_sida": 1}
                        )
                        working[key] = True
                    except Exception:
                        working[key] = False
                    logger.warning(
                        "%s: metadata failure on attempt %d/2 (%s: %s); result "
                        "endpoint %s, so the last accepted document is %s; not "
                        "retried",
                        key,
                        attempt,
                        type(error).__name__,
                        error,
                        "still works" if working[key] else "also fails",
                        "kept" if working[key] else "withheld if one exists",
                    )
                    continue
                failures.pop(key, None)
                working[key] = True
                accepted = published.get(key) or withheld.get(key)
                counts["unchanged" if document == accepted else "rebuilt"] += 1
                logger.debug(
                    "%s: %s",
                    key,
                    "identical to the accepted document; kept"
                    if document == accepted
                    else "new document accepted"
                    if accepted is None
                    else "differs from the accepted document; replaced",
                )
                info.update(
                    verified_at=stamp, mapping_version=MAPPING_VERSION, error=None
                )
                (withheld if key in withheld else published)[key] = document
                progress.update(
                    processed=counts["rebuilt"] + counts["unchanged"],
                    succeeded=counts["rebuilt"] + counts["unchanged"],
                )
                if position % PROGRESS_EVERY == 0 and position < len(pending):
                    logger.info(
                        "Attempt %d/2 progress: %d/%d processed (rebuilt=%d, "
                        "unchanged=%d, failing=%d)",
                        attempt,
                        position,
                        len(pending),
                        counts["rebuilt"],
                        counts["unchanged"],
                        len(failures),
                    )
            if not retry or attempt == 2:
                break
            logger.info(
                "%d endpoint failures; waiting %ss before retrying them once",
                len(retry),
                cooldown,
            )
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            pending = retry
        logger.info(
            "Rebuild finished: rebuilt=%d, unchanged=%d, still failing=%d",
            counts["rebuilt"],
            counts["unchanged"],
            len(failures),
        )
        for key, (stage, error) in sorted(failures.items()):
            state["subjects"][key]["error"] = fail(stage, key, error)
            counts["failed"] += 1
        progress.update(failed=counts["failed"])
        previously_withheld = set(withheld)
        logger.info(
            "Settling publication for %d checked subjects: published only while the "
            "endpoint works and the document was verified within %d days",
            len(settled),
            max_age.days,
        )
        settle(
            sorted(settled),
            published,
            withheld,
            state["subjects"],
            working,
            now,
            max_age,
            counts,
        )
        newly_withheld = sorted(set(withheld) - previously_withheld)
        restored = sorted(previously_withheld - set(withheld))
        for key in newly_withheld:
            logger.warning(
                "%s: withheld (%s); the last accepted document is kept in state",
                key,
                state["subjects"][key].get("withheld_reason"),
            )
        for key in restored:
            logger.info("%s: restored; endpoint works and verification is fresh", key)
        logger.info(
            "Settled: %d published, %d withheld (%d newly, %d still), %d restored",
            len(published),
            len(withheld),
            len(newly_withheld),
            len(withheld) - len(newly_withheld),
            len(restored),
        )
    finally:
        if client is None:
            await connection.close()

    state["mapping_version"] = MAPPING_VERSION
    save(output, state, published, configured)
    status = report_status(pending=0, errors=errors)
    logger.info("Report status %s with %d recorded errors", status, len(errors))
    result = {
        "run_id": uuid4().hex,
        "status": status,
        "continue": False,
        "selection": {
            "subjects": sorted(wanted) if wanted else None,
            "languages": selected_languages,
        },
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
    logger.info(
        "SDB run finished: status=%s, counts=%s, published=%s, withheld=%d, "
        "errors=%d, %d requests, output=%s",
        status,
        counts,
        result["published"],
        len(withheld),
        len(errors),
        getattr(connection, "request_count", 0),
        output,
    )
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting SDB artifact from %s into %s (run %s, status %s)",
        output,
        data,
        result.get("run_id"),
        result.get("status"),
    )
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        logger.error("Refusing artifact: the published baseline changed since the run")
        raise ValueError("Stale SDB artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        logger.error("Refusing artifact: output files do not match the manifest")
        raise ValueError("SDB artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        logger.error(
            "Refusing artifact: unexpected paths %s",
            sorted(set(result["manifest"]) - allowed),
        )
        raise ValueError("Unexpected SDB artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
        logger.debug("Applied %s", relative)
    logger.info("Applied %d SDB artifact files to %s", len(result["manifest"]), data)
    return result

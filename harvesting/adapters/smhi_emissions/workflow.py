"""Monthly rebuild of Nationella emissionsdatabasen with independent substance failures.

One ``getfilteroptions/`` request discovers the sector tree and the substances; it
failing fails the whole run, which then publishes nothing. Each substance's national
``getdata`` probe supplies its years and submission round and is its endpoint check.
A full run is 30 requests, so every run rebuilds every substance; an identical
rebuild keeps its accepted bytes. A substance is published only while its probe
answers and its document was verified within ``max_age_days``; otherwise its last
good document is withheld in state. Only a complete tree in an unbounded run proves
a substance's removal.
"""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from harvesting.adapters.smhi_emissions.api import (
    DATA_URL,
    FILTER_OPTIONS_URL,
    dataset_code,
    probe_parameters,
)
from harvesting.adapters.smhi_emissions.client import SmhiClient, Unreachable
from harvesting.adapters.smhi_emissions.mapping import (
    MAPPING_VERSION,
    build_dataset,
)
from harvesting.adapters.smhi_emissions.source import (
    FilterOptions,
    Substance,
    parse_filter_options,
    parse_probe,
)
from harvesting.adapters.smhi_emissions.state import (
    PROVIDER_CODE,
    REPORT,
    STATE,
    collection,
    load_documents,
    load_state,
    save,
)
from harvesting.artifacts import check_collection_sizes, manifest, report_status
from harvesting.currency import settle
from harvesting.progress import Progress
from harvesting.storage import read, write
from harvesting.validation import validate_metadata
from retrieval.adapters.smhi_emissions import DATASET_PREFIX

ADAPTER = "smhi_emissions"
LANGUAGES = ("sv",)
COUNTS = ("rebuilt", "unchanged", "failed", "withheld", "restored", "removed")


async def build_substance(client, substance: Substance, options: FilterOptions):
    """Probe one substance and return its validated document and submission.

    Raises:
        Unreachable: The probe failed (an endpoint failure).
        Exception: Any other error is a metadata failure; the probe answered.
    """
    body = await client.get_json(DATA_URL, probe_parameters(substance.code))
    probe = parse_probe(body, options, substance.code)
    document = build_dataset(substance, options, probe)
    validate_metadata(document, adapter=ADAPTER)
    return document, probe.submission


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current substances, as ``{"code", "substance_id", "long_name"}``."""
    connection = client or SmhiClient()
    try:
        options = parse_filter_options(await connection.get_json(FILTER_OPTIONS_URL))
    finally:
        if client is None:
            await connection.close()
    return [
        {"code": s.code, "substance_id": s.substance_id, "long_name": s.long_name}
        for s in options.substances.values()
    ]


async def fetch_metadata(record: dict, *, client) -> dict:
    """Build and validate the Swedish document of ``{"code": <substance code>}``."""
    options = parse_filter_options(await client.get_json(FILTER_OPTIONS_URL))
    if record["code"] not in options.substances:
        raise LookupError(f"Unknown SMHI emissions substance {record['code']!r}")
    document, _ = await build_substance(
        client, options.substances[record["code"]], options
    )
    return document


def _select(options: FilterOptions, subjects) -> tuple[list[str], list[str]]:
    """Selected substance codes (exact, else case-insensitive) and unknown names."""
    listed = [*options.substances, *options.quarantined]
    if subjects is None:
        return listed, []
    selected, unknown = [], []
    for name in subjects:
        matches = [c for c in listed if c == name] or [
            c for c in listed if c.casefold() == name.casefold()
        ]
        if len(matches) == 1:
            selected.append(matches[0])
        else:
            unknown.append(name)
    return list(dict.fromkeys(selected)), unknown


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
    """Rebuild every (or each selected) substance into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    if set(configured) - set(LANGUAGES):
        raise ValueError(f"SMHI emissions publishes Swedish only, not {configured}")
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured):
        raise ValueError(f"Unconfigured SMHI emissions languages: {selected_languages}")
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published SMHI emissions data")
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
            options = parse_filter_options(
                await connection.get_json(FILTER_OPTIONS_URL)
            )
            if not options.substances and not options.quarantined:
                raise ValueError("getfilteroptions lists no substances")
        except Exception as error:
            fail("discovery", "filteroptions", error, record=False)
            raise
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
            for key in sorted(known() - listed):
                state["datasets"].pop(key, None)
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1

        working, failures, settled = {}, {}, set()
        pending = []
        for code in selected:
            for language in selected_languages:
                key = f"{language}/{dataset_code(code)}"
                settled.add(key)
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
                if code in options.quarantined:  # keep the last document until stale
                    working[key] = True
                    failures[key] = ("metadata", ValueError(options.quarantined[code]))
                    fail(
                        "metadata", key, failures[key][1], record=False, retrying=False
                    )
                else:
                    pending.append((key, options.substances[code]))

        progress.update(phase="metadata", total=len(pending), processed=0)
        for attempt in (1, 2):
            retry = []
            for key, substance in pending:
                progress.update(current=key, operation=f"rebuild (attempt {attempt})")
                info = state["datasets"][key]
                url = f"{DATA_URL}?{urlencode(probe_parameters(substance.code))}"
                try:
                    document, submission = await build_substance(
                        connection, substance, options
                    )
                except Unreachable as error:
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
                except Exception as error:  # the probe answered: endpoint works
                    working[key], failures[key] = True, ("metadata", error)
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
                working[key] = True
                submissions[substance.code] = submission
                accepted = published.get(key) or withheld.get(key)
                counts["unchanged" if document == accepted else "rebuilt"] += 1
                info.update(
                    verified_at=stamp,
                    mapping_version=MAPPING_VERSION,
                    submission=submission,
                    error=None,
                )
                (withheld if key in withheld else published)[key] = document
                progress.update(
                    processed=counts["rebuilt"] + counts["unchanged"],
                    succeeded=counts["rebuilt"] + counts["unchanged"],
                    failed=len(failures),
                )
            if not retry or attempt == 2:
                break
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
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    if (manifest(data) if data.exists() else {}) != result["base_manifest"]:
        raise ValueError("Stale SMHI emissions artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("SMHI emissions artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        raise ValueError("Unexpected SMHI emissions artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    return result

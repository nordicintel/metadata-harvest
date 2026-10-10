"""Weekly Trafikanalys refresh with independent product and dataset failures.

Trafikanalys publishes no usable update signal and a full harvest is about 400
requests, so every run rebuilds every dataset; an identical rebuild keeps its
accepted bytes. Discovery reads each product's ``/structure`` and, as a deliberate
exception for this temporary adapter, its HTML API documentation page for the
valid API combinations. Each dataset's one-period ``/data`` unit probe is its
endpoint check. A dataset is published only while that endpoint works and its
document was verified within ``max_age_days``; otherwise its last good document is
withheld in state. Only a complete product discovery in an unbounded run proves a
dataset's removal.
"""

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from harvesting.adapters.trafa.api import (
    DATA_URL,
    STRUCTURE_URL,
    api_language,
    documentation_url,
)
from harvesting.adapters.trafa.client import TrafaClient, Unreachable
from harvesting.adapters.trafa.mapping import (
    MAPPING_VERSION,
    build_dataset,
    parse_probe,
    probe_query,
    untranslated_items,
)
from harvesting.adapters.trafa.source import (
    parse_combinations,
    parse_product,
    product_list,
    select_combinations,
)
from harvesting.adapters.trafa.state import (
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

LANGUAGES = ("sv",)
EXCLUDED_PRODUCTS = {
    "ATFT": 'Test product "Annotering test", harvested only on request.'
}
COUNTS = (
    "rebuilt",
    "unchanged",
    "untranslated",
    "failed",
    "withheld",
    "restored",
    "removed",
)


def product_of(key: str) -> str:
    """The product ``UniqueId`` of a ``<language>/<dataset_code>`` key."""
    return key.split("/", 1)[1].split("|", 1)[0]


class _Product:
    """One product's discovery and dataset builds, in every selected language."""

    def __init__(self, client, entry: dict, languages: list[str], probes: dict):
        self.client, self.entry, self.languages = client, entry, languages
        self.probes = probes

    async def structure(self, language: str):
        body = await self.client.get_json(
            STRUCTURE_URL, {"query": self.entry["name"], "lang": api_language(language)}
        )
        return parse_product(body, self.entry["name"])

    async def probe(self, product, combination, language: str):
        query = probe_query(product, combination)
        if (language, query) not in self.probes:
            body = await self.client.get_json(
                DATA_URL, {"query": query, "lang": api_language(language)}
            )
            self.probes[(language, query)] = body
        return parse_probe(self.probes[(language, query)])

    async def discover(self):
        """Return ``(swedish, english, selection)``; Unreachable is an endpoint
        failure, other errors (documentation page, parsing) metadata failures."""
        swedish = await self.structure("sv")
        english = await self.structure("en") if "en" in self.languages else None
        try:
            html = await self.client.get_text(documentation_url(swedish.unique_id))
        except Unreachable as error:  # the page is not the value endpoint
            raise ValueError(f"API documentation page: {error}") from error
        return swedish, english, select_combinations(swedish, parse_combinations(html))

    async def build(self, combination, swedish, english, language: str):
        """The validated document, or None for an untranslated English dataset."""
        swedish_probe = await self.probe(swedish, combination, "sv")
        if language == "sv":
            document = build_dataset(combination, swedish, "sv", swedish_probe)
        elif untranslated_items(combination, english, swedish):
            return None
        else:
            document = build_dataset(
                combination,
                english,
                language,
                await self.probe(english, combination, language),
                reference=swedish,
                reference_probe=swedish_probe,
            )
        validate_metadata(document, adapter="trafa")
        return document


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current product list."""
    connection = client or TrafaClient()
    try:
        return product_list(await connection.get_json(STRUCTURE_URL, {"lang": "SV"}))
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> list[dict]:
    """Build and validate every Swedish dataset of one product-list entry."""
    product = _Product(client, record, ["sv"], {})
    swedish, english, selection = await product.discover()
    return [
        await product.build(combination, swedish, english, "sv")
        for combination in selection.datasets
    ]


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
    """Rebuild every (or each selected) product into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured):
        raise ValueError(f"Unconfigured Trafikanalys languages: {selected_languages}")
    allowed = {STATE, REPORT, *(collection(language) for language in configured)}
    baseline = manifest(data) if data.exists() else {}
    if set(baseline) - allowed:
        raise ValueError("Unexpected file in published Trafikanalys data")
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
    connection = client or TrafaClient()
    progress = Progress("trafa", PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    errors, report = [], {"combinations_without_measures": {}, "excluded": {}}

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        progress.error(stage, resource, resource.split("/", 1)[0], entry["error"])
        return entry

    def known(product_id=None):
        keys = set(published) | set(withheld) | set(state["datasets"])
        return {
            key
            for key in keys
            if key.split("/", 1)[0] in selected_languages
            and (product_id is None or product_of(key) == product_id)
        }

    try:
        progress.update(phase="discovery", current="products")
        listed = product_list(await connection.get_json(STRUCTURE_URL, {"lang": "SV"}))
        if not listed:
            raise ValueError("Trafikanalys lists no products")
        if products is None:
            selected = [e for e in listed if e["unique_id"] not in EXCLUDED_PRODUCTS]
            report["excluded"] = {
                e["unique_id"]: EXCLUDED_PRODUCTS[e["unique_id"]]
                for e in listed
                if e["unique_id"] in EXCLUDED_PRODUCTS
            }
            selected_ids = {e["unique_id"] for e in selected}
            for key in sorted(k for k in known() if product_of(k) not in selected_ids):
                state["datasets"].pop(key, None)
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1
        else:
            wanted = {name.casefold() for name in products}
            selected = [
                e
                for e in listed
                if {e["unique_id"].casefold(), e["name"].casefold()} & wanted
            ]
            names = {k.casefold() for e in listed for k in (e["unique_id"], e["name"])}
            for name in sorted(wanted - names):
                fail("discovery", name, LookupError("Unknown Trafikanalys product"))

        working, failures, settled, probes = {}, {}, set(), {}

        async def harvest(entry) -> bool:
            """Process one product; return whether an endpoint failure occurred."""
            product_id = entry["unique_id"]
            harvester = _Product(connection, entry, selected_languages, probes)
            previous = known(product_id)
            settled.update(previous)
            for key in previous:
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
            try:
                swedish, english, selection = await harvester.discover()
            except Exception as error:
                stage = "endpoint" if isinstance(error, Unreachable) else "metadata"
                for key in previous:  # reported once, for the product
                    working[key] = stage == "metadata"
                failures[f"*/{product_id}"] = (stage, error)
                return stage == "endpoint"
            failures.pop(f"*/{product_id}", None)
            if selection.without_measures:
                report["combinations_without_measures"][product_id] = (
                    selection.without_measures
                )
            current = set()
            endpoint_failed = False
            for combination in selection.datasets:
                for language in selected_languages:
                    key = f"{language}/{combination.dataset_code}"
                    current.add(key)
                    settled.add(key)
                    info = state["datasets"].setdefault(key, {})
                    info["checked_at"] = stamp
                    try:
                        document = await harvester.build(
                            combination, swedish, english, language
                        )
                    except Unreachable as error:
                        working[key], failures[key] = False, ("endpoint", error)
                        endpoint_failed = True
                        continue
                    except Exception as error:  # the probe answered: endpoint works
                        working[key], failures[key] = True, ("metadata", error)
                        continue
                    if document is None:
                        counts["untranslated"] += 1
                        settled.discard(key)
                        state["datasets"].pop(key, None)
                        published.pop(key, None)
                        withheld.pop(key, None)
                        continue
                    failures.pop(key, None)
                    working[key] = True
                    accepted = published.get(key) or withheld.get(key)
                    counts["unchanged" if document == accepted else "rebuilt"] += 1
                    info.update(
                        verified_at=stamp, mapping_version=MAPPING_VERSION, error=None
                    )
                    (withheld if key in withheld else published)[key] = document
            if products is None:
                for key in sorted(previous - current):
                    settled.discard(key)
                    state["datasets"].pop(key, None)
                    failures.pop(key, None)
                    if (
                        published.pop(key, None) or withheld.pop(key, None)
                    ) is not None:
                        counts["removed"] += 1
            return endpoint_failed

        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, entry in enumerate(selected, start=1):
            progress.update(current=entry["unique_id"], operation="rebuild")
            if await harvest(entry):
                retry.append(entry)
            progress.update(processed=number)
        if retry:
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for entry in retry:
                progress.update(current=entry["unique_id"], operation="retry")
                await harvest(entry)
        for key, (stage, error) in sorted(failures.items()):
            entry = fail(stage, key, error)
            counts["failed"] += 1
            for affected in known(key[2:]) if key.startswith("*/") else [key]:
                if affected in state["datasets"]:
                    state["datasets"][affected]["error"] = entry
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
        raise ValueError("Stale Trafikanalys artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Trafikanalys artifact checksum mismatch")
    allowed = {STATE, REPORT, *(collection(language) for language in languages)}
    if not {STATE, REPORT} <= set(result["manifest"]) <= allowed:
        raise ValueError("Unexpected Trafikanalys artifact path")
    check_collection_sizes(output)
    for relative in result["manifest"]:
        target = data / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid4().hex}.tmp")
        shutil.copyfile(output / relative, temporary)
        temporary.replace(target)
    return result

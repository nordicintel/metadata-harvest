"""Weekly Trafikanalys refresh with independent product and dataset failures.

Trafikanalys publishes no usable update signal and a full harvest is about 400
requests, so every run refetches every dataset; identical responses keep their
stored bytes. Discovery reads each product's ``/structure`` and, as a deliberate
exception for this temporary adapter, its HTML API documentation page for the
valid API combinations; both are stored as shared source files. Each dataset's
one-period ``/data`` unit probe is its endpoint check and its stored source
response. A dataset is published only while that endpoint works and its response
was obtained within ``max_age_days``; otherwise it is withheld in state while its
source files remain. Only a complete product discovery in an unbounded run proves
a dataset's removal. Documents are built later by ``parse``.
"""

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.currency import settle
from apps.harvest.metadata_adapters.trafa.api import (
    DATA_URL,
    STRUCTURE_URL,
    api_language,
    documentation_url,
)
from apps.harvest.metadata_adapters.trafa.client import TrafaClient, Unreachable
from apps.harvest.metadata_adapters.trafa.mapping import (
    build_dataset,
    parse_probe,
    probe_query,
    untranslated_items,
)
from apps.harvest.metadata_adapters.trafa.source import (
    parse_combinations,
    parse_product,
    product_list,
    select_combinations,
)
from apps.harvest.metadata_adapters.trafa.state import (
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

logger = logging.getLogger("trafa_logger")

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


def stored_key(key: str) -> str:
    """The source-store key of a ``<language>/<dataset_code>`` state key."""
    language, code = key.split("/", 1)
    return source_key(PROVIDER_CODE, code, language)


def state_key(key: str) -> str:
    _, code, language = split_key(key)
    return f"{language}/{code}"


def _allowed_path(relative: str) -> bool:
    return relative in {STATE, REPORT} or relative.startswith("source/")


class _Product:
    """One product's discovery and dataset probes, in every selected language.

    Source responses are handed to ``store`` as they arrive: structures and the
    documentation page as shared files, each dataset's probe (and, for English,
    the Swedish reference probe) as its own envelope.
    """

    def __init__(
        self, client, entry: dict, languages: list[str], probes: dict, store=None
    ):
        self.client, self.entry, self.languages = client, entry, languages
        self.probes = probes
        self.store = store
        self.stamp = None

    def _share(self, name: str, body) -> None:
        if self.store is not None:
            self.store.put_shared(PROVIDER_CODE, name, body, retrieved_at=self.stamp)

    async def structure(self, language: str):
        body = await self.client.get_json(
            STRUCTURE_URL, {"query": self.entry["name"], "lang": api_language(language)}
        )
        self._share(f"structure.{self.entry['unique_id']}.{language}.json", body)
        return parse_product(body, self.entry["name"])

    async def probe(self, product, combination, language: str):
        """The raw ``/data`` body of the one-period probe, cached per query."""
        query = probe_query(product, combination)
        if (language, query) not in self.probes:
            logger.debug(
                "Probing %s (%s): %s", combination.dataset_code, language, query
            )
            body = await self.client.get_json(
                DATA_URL, {"query": query, "lang": api_language(language)}
            )
            self.probes[(language, query)] = body
        else:
            logger.debug("Reusing cached %s probe for %s", language, query)
        return self.probes[(language, query)]

    async def discover(self):
        """Return ``(swedish, english, selection)``; Unreachable is an endpoint
        failure, other errors (documentation page, parsing) metadata failures."""
        swedish = await self.structure("sv")
        english = await self.structure("en") if "en" in self.languages else None
        logger.debug(
            "Product %s: structure read (sv%s), %d variables, %d measures",
            self.entry["unique_id"],
            " and en" if english else "",
            len(swedish.variables),
            len(swedish.measures),
        )
        try:
            html = await self.client.get_text(documentation_url(swedish.unique_id))
        except Unreachable as error:  # the page is not the value endpoint
            raise ValueError(f"API documentation page: {error}") from error
        self._share(f"documentation.{swedish.unique_id}.html", html)
        return swedish, english, select_combinations(swedish, parse_combinations(html))

    async def responses(self, combination, swedish, english, language: str):
        """The source responses of one dataset, or None when it is untranslated."""
        swedish_probe = await self.probe(swedish, combination, "sv")
        if language == "sv":
            return {"probe": swedish_probe}
        if untranslated_items(combination, english, swedish):
            return None
        return {
            "probe": await self.probe(english, combination, language),
            "reference_probe": swedish_probe,
        }

    async def build(self, combination, swedish, english, language: str):
        """The document built in memory, or None for an untranslated dataset."""
        responses = await self.responses(combination, swedish, english, language)
        if responses is None:
            return None
        if language == "sv":
            return build_dataset(
                combination, swedish, "sv", parse_probe(responses["probe"])
            )
        return build_dataset(
            combination,
            english,
            language,
            parse_probe(responses["probe"]),
            reference=swedish,
            reference_probe=parse_probe(responses["reference_probe"]),
        )


async def discover_provider(provider: dict, *, client=None) -> list[dict]:
    """The current product list."""
    connection = client or TrafaClient()
    try:
        products = product_list(
            await connection.get_json(STRUCTURE_URL, {"lang": "SV"})
        )
        logger.info("Trafikanalys lists %d products", len(products))
        return products
    finally:
        if client is None:
            await connection.close()


async def fetch_metadata(record: dict, *, client) -> list[dict]:
    """Build every Swedish dataset of one product-list entry in memory.

    This typed boundary serves tests and live checks; workflows store responses
    and leave document building to ``parse``.
    """
    product = _Product(client, record, ["sv"], {})
    swedish, english, selection = await product.discover()
    logger.info(
        "Product %s: building %d Swedish datasets",
        record["unique_id"],
        len(selection.datasets),
    )
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
    """Refetch every (or each selected) product into a separate worker output."""
    data, output = Path(data), Path(output)
    if data.resolve() == output.resolve() or data.resolve() in output.resolve().parents:
        raise ValueError("Worker output must be separate from published data")
    configured = list(provider.get("languages", LANGUAGES))
    selected_languages = list(languages or configured)
    if set(selected_languages) - set(configured):
        raise ValueError(f"Unconfigured Trafikanalys languages: {selected_languages}")
    baseline = harvest_manifest(data) if data.exists() else {}
    if any(not _allowed_path(relative) for relative in baseline):
        raise ValueError("Unexpected file in published Trafikanalys data")
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
        key = state_key(stored)
        status = state["datasets"].get(key, {}).get("status")
        (withheld if status == "withheld" else published)[key] = True
    now = clock or datetime.now(UTC)
    stamp = now.isoformat()
    max_age = timedelta(days=provider.get("max_age_days", 60))
    cooldown = provider.get("retry_cooldown_seconds", 60)
    connection = client or TrafaClient()
    progress = Progress("trafa", PROVIDER_CODE)
    counts = dict.fromkeys(COUNTS, 0)
    logger.info(
        "Trafikanalys run starting: %s (requested products: %s); languages %s "
        "(configured %s); max_age %s days, retry cooldown %ss; baseline "
        "%d published, %d withheld, %d tracked datasets",
        "full rebuild of every product (no update signal is published)"
        if products is None
        else "rebuild restricted to the requested products",
        "all" if products is None else sorted(products),
        selected_languages,
        configured,
        max_age.days,
        cooldown,
        len(published),
        len(withheld),
        len(state["datasets"]),
    )
    errors, report = [], {"combinations_without_measures": {}, "excluded": {}}

    def fail(stage, resource, error):
        entry = {
            "stage": stage,
            "resource": resource,
            "error": f"{type(error).__name__}: {error}",
        }
        errors.append(entry)
        logger.error("%s failure for %s: %s", stage, resource, entry["error"])
        progress.error(stage, resource, resource.split("/", 1)[0], entry["error"])
        return entry

    def _forget_product(product_id):
        """Drop a product's shared files once none of its datasets remain."""
        if any(product_of(key) == product_id for key in known()):
            return
        for language in configured:
            store.remove_shared(
                PROVIDER_CODE, f"structure.{product_id}.{language}.json"
            )
        store.remove_shared(PROVIDER_CODE, f"documentation.{product_id}.html")

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
        listing = await connection.get_json(STRUCTURE_URL, {"lang": "SV"})
        listed = product_list(listing)
        logger.info("Trafikanalys lists %d products", len(listed))
        if not listed:
            raise ValueError("Trafikanalys lists no products")
        store.put_shared(PROVIDER_CODE, "products.json", listing, retrieved_at=stamp)
        if products is None:
            selected = [e for e in listed if e["unique_id"] not in EXCLUDED_PRODUCTS]
            report["excluded"] = {
                e["unique_id"]: EXCLUDED_PRODUCTS[e["unique_id"]]
                for e in listed
                if e["unique_id"] in EXCLUDED_PRODUCTS
            }
            selected_ids = {e["unique_id"] for e in selected}
            logger.info(
                "Full run: selected %d of %d listed products; excluded %s",
                len(selected),
                len(listed),
                sorted(report["excluded"]) or "none",
            )
            orphans = sorted(k for k in known() if product_of(k) not in selected_ids)
            for key in orphans:
                state["datasets"].pop(key, None)
                store.remove(stored_key(key))
                if (published.pop(key, None) or withheld.pop(key, None)) is not None:
                    counts["removed"] += 1
                    logger.debug("Removed %s: its product is no longer harvested", key)
            for product_id in {product_of(key) for key in orphans}:
                _forget_product(product_id)
            if orphans:
                logger.info(
                    "Dropped %d known datasets whose product is no longer listed "
                    "or is excluded (%d accepted documents removed)",
                    len(orphans),
                    counts["removed"],
                )
        else:
            wanted = {name.casefold() for name in products}
            selected = [
                e
                for e in listed
                if {e["unique_id"].casefold(), e["name"].casefold()} & wanted
            ]
            names = {k.casefold() for e in listed for k in (e["unique_id"], e["name"])}
            logger.info(
                "Restricted run: %d of %d listed products match the requested %s; "
                "datasets of other products and removals are left untouched",
                len(selected),
                len(listed),
                sorted(products),
            )
            for name in sorted(wanted - names):
                fail("discovery", name, LookupError("Unknown Trafikanalys product"))

        working, failures, settled, probes = {}, {}, set(), {}

        async def harvest(entry) -> bool:
            """Process one product; return whether an endpoint failure occurred."""
            product_id = entry["unique_id"]
            harvester = _Product(connection, entry, selected_languages, probes, store)
            harvester.stamp = stamp
            previous = known(product_id)
            before = dict(counts)
            logger.debug(
                "Product %s: %d datasets already known before rebuild",
                product_id,
                len(previous),
            )
            settled.update(previous)
            for key in previous:
                state["datasets"].setdefault(key, {})["checked_at"] = stamp
            try:
                swedish, english, selection = await harvester.discover()
            except Exception as error:
                stage = "endpoint" if isinstance(error, Unreachable) else "metadata"
                logger.warning(
                    "Product %s: discovery failed at the %s stage (%s: %s); "
                    "its %d known datasets keep their accepted documents%s",
                    product_id,
                    stage,
                    type(error).__name__,
                    error,
                    len(previous),
                    "; will retry after the cooldown" if stage == "endpoint" else "",
                )
                for key in previous:  # reported once, for the product
                    working[key] = stage == "metadata"
                failures[f"*/{product_id}"] = (stage, error)
                return stage == "endpoint"
            failures.pop(f"*/{product_id}", None)
            if selection.without_measures:
                report["combinations_without_measures"][product_id] = (
                    selection.without_measures
                )
                logger.info(
                    "Product %s: skipped %d listed combinations without measures",
                    product_id,
                    len(selection.without_measures),
                )
            logger.debug(
                "Product %s: %d listed combinations reduce to %d maximal datasets",
                product_id,
                sum(len(c.listed) for c in selection.datasets),
                len(selection.datasets),
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
                        responses = await harvester.responses(
                            combination, swedish, english, language
                        )
                    except Unreachable as error:
                        working[key], failures[key] = False, ("endpoint", error)
                        endpoint_failed = True
                        logger.warning(
                            "Dataset %s: value endpoint unreachable (%s)", key, error
                        )
                        continue
                    if responses is None:
                        logger.debug(
                            "Dataset %s: skipped, English labels are not real "
                            "translations; any earlier source entry is dropped",
                            key,
                        )
                        counts["untranslated"] += 1
                        settled.discard(key)
                        state["datasets"].pop(key, None)
                        published.pop(key, None)
                        withheld.pop(key, None)
                        store.remove(stored_key(key))
                        continue
                    working[key] = True  # the probe answered: endpoint works
                    stored = store.get(stored_key(key))
                    try:
                        store.put(
                            stored_key(key),
                            responses=responses,
                            updated=None,
                            retrieved_at=stamp,
                        )
                    except Rejected as error:
                        failures[key] = ("metadata", error)
                        logger.warning(
                            "Dataset %s: response not storable (%s); the endpoint "
                            "answered, so any stored source entry stays as it is",
                            key,
                            error,
                        )
                        continue
                    failures.pop(key, None)
                    unchanged = stored is not None and stored["responses"] == responses
                    counts["unchanged" if unchanged else "rebuilt"] += 1
                    logger.debug(
                        "Dataset %s: %s",
                        key,
                        "identical to the stored responses, bytes kept"
                        if unchanged
                        else "stored"
                        + (" (new)" if stored is None else " (content changed)"),
                    )
                    info.update(verified_at=stamp, error=None)
                    (withheld if key in withheld else published)[key] = True
            if products is None:
                for key in sorted(previous - current):
                    settled.discard(key)
                    state["datasets"].pop(key, None)
                    failures.pop(key, None)
                    store.remove(stored_key(key))
                    if (
                        published.pop(key, None) or withheld.pop(key, None)
                    ) is not None:
                        counts["removed"] += 1
                    logger.debug(
                        "Dataset %s: removed, complete discovery of %s no longer "
                        "lists it",
                        key,
                        product_id,
                    )
            else:
                missing = previous - current
                if missing:
                    logger.info(
                        "Product %s: %d known datasets are not in this discovery; "
                        "kept because the run is restricted to named products",
                        product_id,
                        len(missing),
                    )
            logger.info(
                "Product %s: %d datasets discovered; %d rebuilt, %d unchanged, "
                "%d untranslated, %d removed",
                product_id,
                len(selection.datasets),
                counts["rebuilt"] - before["rebuilt"],
                counts["unchanged"] - before["unchanged"],
                counts["untranslated"] - before["untranslated"],
                counts["removed"] - before["removed"],
            )
            return endpoint_failed

        logger.info("Refetching %d products", len(selected))
        progress.update(phase="metadata", total=len(selected), processed=0)
        retry = []
        for number, entry in enumerate(selected, start=1):
            progress.update(current=entry["unique_id"], operation="rebuild")
            if await harvest(entry):
                retry.append(entry)
            progress.update(processed=number)
        if retry:
            logger.warning(
                "Retrying %d products with endpoint failures after a %ss cooldown: %s",
                len(retry),
                cooldown,
                [entry["unique_id"] for entry in retry],
            )
            progress.update(operation="retry cooldown", current=None)
            await asyncio.sleep(cooldown)
            for entry in retry:
                progress.update(current=entry["unique_id"], operation="retry")
                if await harvest(entry):
                    logger.warning(
                        "Product %s: still has endpoint failures after the retry",
                        entry["unique_id"],
                    )
        for key, (stage, error) in sorted(failures.items()):
            entry = fail(stage, key, error)
            counts["failed"] += 1
            for affected in known(key[2:]) if key.startswith("*/") else [key]:
                if affected in state["datasets"]:
                    state["datasets"][affected]["error"] = entry
        progress.update(failed=counts["failed"])
        logger.info(
            "Settling %d datasets: published only while the endpoint works and "
            "the response was obtained within %d days",
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
        outcomes = {}
        for key in sorted(settled):
            info = state["datasets"].get(key, {})
            outcome = info.get("withheld_reason") or info.get("status")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if info.get("status") != "published":
                logger.debug("Dataset %s settled as %s", key, outcome)
        logger.info("Settled outcomes: %s", dict(sorted(outcomes.items(), key=str)))
        if withheld:
            logger.warning(
                "%d datasets are withheld (%d newly, %d restored this run): %s",
                len(withheld),
                counts["withheld"],
                counts["restored"],
                sorted({product_of(key) for key in withheld}),
            )
    finally:
        if client is None:
            await connection.close()

    store.save()
    save(output, state)
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
    logger.info(
        "Trafikanalys run finished with status %s after %s requests: counts %s; "
        "published %s; %d withheld; %d errors",
        status,
        getattr(connection, "request_count", "unknown"),
        counts,
        result["published"],
        len(withheld),
        len(errors),
    )
    return result


def collect(data: Path, output: Path, *, languages=LANGUAGES) -> dict:
    """Verify the artifact against the unchanged published baseline, then apply it."""
    data, output = Path(data), Path(output)
    result = read(output / "result.json")
    logger.info(
        "Collecting Trafikanalys artifact (run %s, status %s)",
        result.get("run_id"),
        result.get("status"),
    )
    if (harvest_manifest(data) if data.exists() else {}) != result["base_manifest"]:
        logger.error("Refusing the artifact: published baseline changed since the run")
        raise ValueError("Stale Trafikanalys artifact: published baseline changed")
    if manifest(output) != result["manifest"]:
        raise ValueError("Trafikanalys artifact checksum mismatch")
    if not {STATE, REPORT} <= set(result["manifest"]) or not all(
        _allowed_path(relative) for relative in result["manifest"]
    ):
        raise ValueError("Unexpected Trafikanalys artifact path")
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
    logger.info("Applied %d artifact files", len(result["manifest"]))
    return result

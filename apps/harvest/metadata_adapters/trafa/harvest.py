"""Weekly Trafikanalys refresh with independent product and dataset failures.

Trafikanalys publishes no usable update signal and a full harvest is about 400
requests, so every run refetches every dataset; identical responses keep their
stored bytes. The product list is read once; each product is one scope whose
discovery reads its ``/structure`` and, as a deliberate exception for this
temporary adapter, its HTML API documentation page for the valid API
combinations; both are stored as shared source files. Each dataset's one-period
``/data`` unit probe is its endpoint check and its stored source response.
"""

from apps.harvest.metadata_adapters.trafa.client import (
    DATA_URL,
    PROVIDER_CODE,
    STRUCTURE_URL,
    TrafaClient,
    Unreachable,
    api_language,
    documentation_url,
)
from apps.harvest.metadata_adapters.trafa.mapping import (
    probe_query,
    untranslated_items,
)
from apps.harvest.metadata_adapters.trafa.source import (
    parse_combinations,
    parse_product,
    product_list,
    select_combinations,
)
from harvest.lifecycle import Adapter, Dropped, Run, Scope, Stored, Task

EXCLUDED_PRODUCTS = {
    "ATFT": 'Test product "Annotering test", harvested only on request.'
}


def product_of(key: str) -> str:
    """The product ``UniqueId`` of a ``<language>/<dataset_code>`` key."""
    return key.split("/", 1)[1].split("|", 1)[0]


class Product:
    """One product's discovery and dataset probes, in every selected language.

    Source responses are handed to the run's store as they arrive: structures and
    the documentation page as shared files; probes are cached per query so the
    Swedish reference probe of an English dataset is requested once.
    """

    def __init__(self, run: Run, entry: dict):
        self.run, self.entry = run, entry
        self.probes: dict = {}
        self.swedish = self.english = None

    def _share(self, name: str, body) -> None:
        self.run.store.put_shared(
            PROVIDER_CODE, name, body, retrieved_at=self.run.stamp
        )

    async def structure(self, language: str):
        body = await self.run.client.get_json(
            STRUCTURE_URL, {"query": self.entry["name"], "lang": api_language(language)}
        )
        self._share(f"structure.{self.entry['unique_id']}.{language}.json", body)
        return parse_product(body, self.entry["name"])

    async def probe(self, product, combination, language: str):
        """The raw ``/data`` body of the one-period probe, cached per query."""
        query = probe_query(product, combination)
        if (language, query) not in self.probes:
            self.probes[(language, query)] = await self.run.client.get_json(
                DATA_URL, {"query": query, "lang": api_language(language)}
            )
        return self.probes[(language, query)]

    async def discover(self):
        """Return the selected combinations; Unreachable is an endpoint failure,
        other errors (documentation page, parsing) metadata failures."""
        self.swedish = await self.structure("sv")
        if "en" in self.run.languages:
            self.english = await self.structure("en")
        try:
            html = await self.run.client.get_text(
                documentation_url(self.swedish.unique_id)
            )
        except Unreachable as error:  # the page is not the value endpoint
            raise ValueError(f"API documentation page: {error}") from error
        self._share(f"documentation.{self.swedish.unique_id}.html", html)
        return select_combinations(self.swedish, parse_combinations(html))

    async def responses(self, combination, language: str):
        """The source responses of one dataset, or None when it is untranslated."""
        swedish_probe = await self.probe(self.swedish, combination, "sv")
        if language == "sv":
            return {"probe": swedish_probe}
        if untranslated_items(combination, self.english, self.swedish):
            return None
        return {
            "probe": await self.probe(self.english, combination, language),
            "reference_probe": swedish_probe,
        }


async def discover(run: Run) -> list[Scope]:
    listing = await run.client.get_json(STRUCTURE_URL, {"lang": "SV"})
    listed = product_list(listing)
    if not listed:
        raise ValueError("Trafikanalys lists no products")
    run.store.put_shared(
        PROVIDER_CODE, "products.json", listing, retrieved_at=run.stamp
    )
    products = run.selection.get("products")
    if products is None:
        selected = [e for e in listed if e["unique_id"] not in EXCLUDED_PRODUCTS]
        run.report["excluded"] = {
            e["unique_id"]: EXCLUDED_PRODUCTS[e["unique_id"]]
            for e in listed
            if e["unique_id"] in EXCLUDED_PRODUCTS
        }
    else:
        wanted = {name.casefold(): name for name in products}
        selected = [
            e
            for e in listed
            if {e["unique_id"].casefold(), e["name"].casefold()} & wanted.keys()
        ]
        names = {k.casefold() for e in listed for k in (e["unique_id"], e["name"])}
        for name in sorted(wanted.keys() - names):
            run.fail(
                "discovery",
                f"*/{wanted[name]}",
                LookupError("Unknown Trafikanalys product"),
            )
    run.report["combinations_without_measures"] = {}
    scopes = []
    for entry in selected:
        product = Product(run, entry)

        async def load(product=product) -> list[Task]:
            selection = await product.discover()
            if selection.without_measures:
                run.report["combinations_without_measures"][
                    product.entry["unique_id"]
                ] = selection.without_measures
            return [
                Task(f"{language}/{combination.dataset_code}", (product, combination))
                for combination in selection.datasets
                for language in run.languages
            ]

        scopes.append(Scope(entry["unique_id"], load))
    return scopes


async def fetch(run: Run, task: Task) -> Stored | Dropped:
    product, combination = task.item
    responses = await product.responses(combination, task.key.split("/", 1)[0])
    if responses is None:  # English labels are not real translations
        return Dropped("untranslated")
    return Stored(responses=responses)


def forget(run: Run, key: str) -> None:
    """Drop a product's shared files once none of its datasets remain."""
    product_id = product_of(key)
    if any(product_of(k) == product_id for k in run.known()):
        return
    for language in run.binding.options.get("languages", ADAPTER.languages):
        run.store.remove_shared(
            PROVIDER_CODE, f"structure.{product_id}.{language}.json"
        )
    run.store.remove_shared(PROVIDER_CODE, f"documentation.{product_id}.html")


ADAPTER = Adapter(
    name="trafa",
    provider_code=PROVIDER_CODE,
    workflow_file="harvest-trafa.yml",
    client=lambda binding: TrafaClient(),
    discover=discover,
    fetch=fetch,
    options={"product": "products"},
    languages=("sv", "en"),
    counts=("untranslated",),
    forget=forget,
    scope_of=product_of,
)

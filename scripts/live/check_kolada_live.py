"""Opt-in live sample: six KPIs, at most twelve documents; never a corpus scan.

Runs the adapter for the sampled KPIs from an empty state into a temporary
directory (the listings and one rescan per KPI scope), parses the stored files into
documents (validated), then retrieves each one's newest year for one area and
checks the fragment.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import aiohttp

import registry
from apps.harvest.metadata_adapters.kolada import parse
from apps.harvest.metadata_adapters.kolada.client import KoladaClient
from apps.harvest.metadata_adapters.kolada.harvest import ADAPTER, load_state
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.kolada import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

KPIS = ("N00003", "N78110", "N78111", "U30750", "U60495", "N26527")


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = KoladaClient(binding.options, base_url=binding.endpoint_url)
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"kpis": list(KPIS)},
            client=client,
        )
    finally:
        await client.close()
    store = SourceStore(root / "out/source", ADAPTER.name)
    documents = []
    for _key, document in sorted(
        parse.documents(store, load_state(root / "out"), [binding])
    ):
        if isinstance(document, Exception):
            raise document
        validate_metadata(document, adapter=ADAPTER.name)
        documents.append(document)
    if len(documents) > 12:
        raise ValueError("Expected at most twelve documents")
    slots = asyncio.Semaphore(4)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as session:

        async def probe(document, ordinal):
            native = document["extension"]["kolada"]
            scope = native["scope"]
            axes = {
                key: category_codes(node["category"])
                for key, node in document["dimension"].items()
            }
            area = axes[scope][:1]
            groups = [code for code in axes[scope] if code.startswith("G")]
            if groups:
                area += groups[:1]
            selection = {"year": axes["year"][-1:], scope: area}
            if "gender" in axes:
                selection["gender"] = axes["gender"]
            shared = document["extension"]["nordicintel"]
            entry = {
                "dataset": shared["dataset_code"],
                "selection": selection,
                "metadata_validation": "passed",
            }
            await asyncio.sleep(ordinal * 0.225)
            try:
                async with slots:
                    fragment = await retrieve(
                        session,
                        provider_code=shared["provider_code"],
                        dataset_code=shared["dataset_code"],
                        language="sv",
                        selection=selection,
                        data_url=shared["data_url"],
                        metadata_url=shared["metadata_url"],
                        metadata={
                            REQUIRED_METADATA[0]: document["dimension"],
                            REQUIRED_METADATA[1]: native,
                        },
                    )
                validate_retrieval(fragment, selection=selection)
                entry.update(
                    returned_values=len(fragment["value"]),
                    returned_flags=len(fragment.get("status", {})),
                    status="passed",
                )
            except Exception as error:
                entry.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{entry['status']}: {shared['dataset_code']}", flush=True)
            return entry

        report = await asyncio.gather(
            *(probe(document, i) for i, document in enumerate(documents))
        )
    return {
        "verification": "bounded sample only",
        "run": {k: result[k] for k in ("status", "counts", "errors")},
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("output/kolada-live.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kolada-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

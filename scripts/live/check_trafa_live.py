"""Opt-in live sample: two Trafikanalys products; never a corpus scan.

Runs the adapter for the sampled products into a temporary directory, parses the
stored files into documents (validating them), then retrieves the first dataset's
newest period with every other dimension at its first category.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.trafa import parse
from apps.harvest.metadata_adapters.trafa.client import TrafaClient
from apps.harvest.metadata_adapters.trafa.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.trafa import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

PRODUCTS = ("T10011", "T10033")


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = TrafaClient()
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"products": list(PRODUCTS), "languages": ["sv"]},
            client=client,
        )
        store = SourceStore(root / "out/source", ADAPTER.name)
        state = lifecycle.load_state(root / "out", ADAPTER.collection)
        report, retrieved = [], False
        for key, document in sorted(parse.documents(store, state, [binding])):
            item = {"dataset": key}
            try:
                if isinstance(document, Exception):
                    raise document
                validate_metadata(document, adapter=ADAPTER.name)
                item["metadata_validation"] = "passed"
                if not retrieved:
                    retrieved = True
                    time = (document.get("role", {}).get("time") or [None])[0]
                    selection = {
                        name: category_codes(node["category"])[:1]
                        for name, node in document["dimension"].items()
                    }
                    if time:
                        selection[time] = category_codes(
                            document["dimension"][time]["category"]
                        )[-1:]
                    shared = document["extension"]["nordicintel"]
                    fragment = await retrieve(
                        client.session,
                        provider_code=shared["provider_code"],
                        dataset_code=shared["dataset_code"],
                        language="sv",
                        selection=selection,
                        data_url=shared["data_url"],
                        metadata_url=shared["metadata_url"],
                        metadata={
                            REQUIRED_METADATA[0]: document["id"],
                            REQUIRED_METADATA[1]: document["extension"]["trafa"],
                        },
                    )
                    validate_retrieval(fragment, selection=selection)
                    item.update(
                        selection=selection, returned_values=len(fragment["value"])
                    )
                item["status"] = "passed"
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {key}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "run": {k: result[k] for k in ("status", "counts", "errors")},
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("output/trafa-live.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="trafa-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

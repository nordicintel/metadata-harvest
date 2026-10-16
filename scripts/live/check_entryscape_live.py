"""Opt-in live sample: one EntryScape instance's discovery and two retrievals.

Runs the adapter for one provider into a temporary directory under a budget of
twelve requests, parses the stored tables into datasets, then retrieves two small
selections. Run it from a GitHub runner, never from a host the EntryScape
instances already block.
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import aiohttp

import registry
from apps.harvest.metadata_adapters.entryscape import parse
from apps.harvest.metadata_adapters.entryscape.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.entryscape import REQUIRED_METADATA, retrieve
from schema.validation import validate_retrieval

PROVIDER = "catalogalese"


async def check(provider: str, root: Path) -> dict:
    bindings = registry.load().bindings(ADAPTER.name)
    result = await lifecycle.run(
        ADAPTER,
        bindings,
        root / "data",
        root / "out",
        selection={"selected": [provider], "max_requests": 12, "preflight": False},
    )
    report = {
        "provider": provider,
        "run": {k: result[k] for k in ("status", "counts", "errors", "requests")},
    }
    store = SourceStore(root / "out/source", ADAPTER.name)
    state = lifecycle.load_state(root / "out", ADAPTER.collection)
    documents = [
        d
        for _, d in parse.documents(store, state, bindings)
        if not isinstance(d, Exception)
    ]
    documents.sort(key=lambda d: d["extension"]["entryscape"]["rowcount"])
    report["retrievals"] = []
    async with aiohttp.ClientSession(
        headers={"User-Agent": "nordicintel-metadata-harvest/entryscape live check"}
    ) as session:
        for document in documents[:2]:
            shared = document["extension"]["nordicintel"]
            selection = {
                code: category_codes(document["dimension"][code]["category"])[:2]
                for code in document["id"]
            }
            item = {"dataset": shared["dataset_code"], "selection": selection}
            try:
                fragment = await retrieve(
                    session,
                    provider_code=shared["provider_code"],
                    dataset_code=shared["dataset_code"],
                    language="sv",
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared.get("metadata_url"),
                    metadata={
                        REQUIRED_METADATA[0]: document["id"],
                        REQUIRED_METADATA[1]: document["dimension"],
                        REQUIRED_METADATA[2]: document["extension"]["entryscape"],
                    },
                )
                validate_retrieval(fragment, selection=selection)
                item.update(status="passed", returned_values=len(fragment["value"]))
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {shared['dataset_code']}", flush=True)
            report["retrievals"].append(item)
    return {
        **report,
        "verification": "bounded sample only",
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=PROVIDER)
    parser.add_argument(
        "--report", type=Path, default=Path("output/entryscape-live.json")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="entryscape-live-") as temporary:
        result = asyncio.run(check(args.provider, Path(temporary)))
    write(args.report, result)
    print(json.dumps(result["run"], ensure_ascii=False))
    raise SystemExit(any(r["status"] != "passed" for r in result["retrievals"]))

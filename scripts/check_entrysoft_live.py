"""Opt-in live sample: one EntryScape instance's discovery and two retrievals.

Spends at most about ten requests. Run it from a GitHub runner, never from a
host the EntryScape instances already block.
"""

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp

from harvesting.adapters.entrysoft.client import Client, Pacer
from harvesting.adapters.entrysoft.state import collection_store
from harvesting.adapters.entrysoft.workflow import discover_provider
from harvesting.configuration import providers_for_adapter
from harvesting.storage import write
from harvesting.validation import validate_retrieval
from retrieval._semantics import category_codes
from retrieval.adapters.entrysoft import REQUIRED_METADATA, retrieve

PROVIDER = "catalogalese"


async def check(provider: dict, data: Path) -> dict:
    report = {"provider": provider["code"]}
    pacer = Pacer(1.0, budget=12, max_throttles=1)
    async with Client(provider["base_url"], pacer=pacer) as client:
        scope = await discover_provider(provider, client=client)
        report["discovery"] = {
            "complete": scope["complete"],
            "tables": len(scope["records"]),
            "errors": scope["errors"],
            "requests": client.requests,
        }
    documents = [
        d
        for d in collection_store(data).records.values()
        if d["extension"]["nordicintel"]["provider_code"] == provider["code"]
    ]
    documents.sort(key=lambda d: d["extension"]["entrysoft"]["rowcount"])
    report["retrievals"] = []
    async with aiohttp.ClientSession(
        headers={"User-Agent": "nordicintel-metadata-harvest/entrysoft live check"}
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
                        REQUIRED_METADATA[2]: document["extension"]["entrysoft"],
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
    parser.add_argument("--providers", type=Path, default=None)
    parser.add_argument("--data", type=Path, default=Path("data/entrysoft"))
    parser.add_argument(
        "--report", type=Path, default=Path("output/entrysoft-live.json")
    )
    args = parser.parse_args()
    provider = next(
        p
        for p in providers_for_adapter("entrysoft", legacy_path=args.providers)
        if p["code"] == args.provider
    )
    result = asyncio.run(check(provider, args.data))
    write(args.report, result)
    print(json.dumps(result["discovery"], ensure_ascii=False))
    raise SystemExit(any(r["status"] != "passed" for r in result["retrievals"]))

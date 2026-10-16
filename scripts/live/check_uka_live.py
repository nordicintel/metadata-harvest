"""Opt-in live sample: three UKÄ indicators; never a corpus scan.

Rebuilds each sampled document from UKÄ (validating it), then retrieves its
newest period with every breakdown at ``Total`` and checks the fragment.
"""

import argparse
import asyncio
from dataclasses import asdict
from pathlib import Path

import aiohttp

import registry
from apps.harvest.metadata_adapters.uka.client import TIMEOUT, USER_AGENT, UkaClient
from apps.harvest.metadata_adapters.uka.source import parse_catalogue
from apps.harvest.metadata_adapters.uka.workflow import fetch_metadata
from apps.harvest.storage import write
from registry import ServiceBinding
from retrieval.data_adapters.uka import REQUIRED_METADATA, retrieve
from schema.validation import validate_retrieval

INDICATORS = {"15", "72", "97"}


async def check(binding: ServiceBinding) -> dict:
    report = []
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    ) as session:
        client = UkaClient(
            binding.options, base_url=binding.endpoint_url, session=session
        )
        entries = [
            e
            for e in parse_catalogue(await client.catalogue())
            if str(e.id) in INDICATORS
        ]
        if {str(e.id) for e in entries} != INDICATORS:
            raise ValueError("A sampled indicator is missing from UKÄ's catalogue")
        for entry in entries:
            item = {"dataset": str(entry.id)}
            try:
                document = await fetch_metadata(asdict(entry), client=client)
                time = document["role"]["time"][0]
                selection = {
                    time: document["dimension"][time]["category"]["index"][-1:]
                }
                shared = document["extension"]["nordicintel"]
                result = await retrieve(
                    session,
                    provider_code="uka",
                    dataset_code=shared["dataset_code"],
                    language="sv",
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared["metadata_url"],
                    metadata={REQUIRED_METADATA[0]: document["role"]},
                )
                validate_retrieval(result, selection=selection)
                item.update(
                    selection=selection,
                    metadata_validation="passed",
                    returned_values=len(result["value"]),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {entry.id}", flush=True)
            report.append(item)
    return {
        "verification": "bounded sample only",
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("output/uka-live.json"))
    args = parser.parse_args()
    result = asyncio.run(check(registry.load().bindings("uka")[0]))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

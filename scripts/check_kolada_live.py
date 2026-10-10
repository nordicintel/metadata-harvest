"""Opt-in live sample: six KPIs, at most twelve documents; never a corpus scan."""

import argparse
import asyncio
from pathlib import Path

import aiohttp

from harvesting.adapters.kolada.state import COLLECTION, records
from harvesting.storage import write
from harvesting.validation import validate_metadata, validate_retrieval
from retrieval._semantics import category_codes
from retrieval.adapters.kolada import REQUIRED_METADATA, retrieve

KPIS = {"N00003", "N78110", "N78111", "U30750", "U60495", "N26527"}


async def check(root: Path) -> dict:
    sample = [
        d
        for d in records(root / COLLECTION)
        if d["extension"]["kolada"]["kpi_id"] in KPIS
    ]
    if len(sample) > 12 or {d["extension"]["kolada"]["kpi_id"] for d in sample} != KPIS:
        raise ValueError("Expected six KPIs and at most twelve documents")
    for document in sample:
        validate_metadata(document, adapter="kolada")
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
                    result = await retrieve(
                        session,
                        provider_code="kolada",
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
                validate_retrieval(result, selection=selection)
                entry.update(
                    returned_values=len(result["value"]),
                    returned_flags=len(result.get("status", {})),
                    status="passed",
                )
            except Exception as error:
                entry.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{entry['status']}: {shared['dataset_code']}", flush=True)
            return entry

        report = await asyncio.gather(
            *(probe(document, i) for i, document in enumerate(sample))
        )
    return {
        "verification": "bounded sample only",
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/kolada"))
    parser.add_argument("--report", type=Path, default=Path("output/kolada-live.json"))
    args = parser.parse_args()
    result = asyncio.run(check(args.data))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

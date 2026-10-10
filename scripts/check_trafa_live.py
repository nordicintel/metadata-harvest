"""Opt-in live sample: two Trafikanalys products; never a corpus scan.

Rebuilds each sampled product's Swedish datasets (validating them), then retrieves
the first dataset's newest period with every other dimension at its first category.
"""

import argparse
import asyncio
from pathlib import Path

from harvesting.adapters.trafa.api import STRUCTURE_URL
from harvesting.adapters.trafa.client import TrafaClient
from harvesting.adapters.trafa.source import product_list
from harvesting.adapters.trafa.workflow import fetch_metadata
from harvesting.storage import write
from harvesting.validation import validate_retrieval
from retrieval._semantics import category_codes
from retrieval.adapters.trafa import REQUIRED_METADATA, retrieve

PRODUCTS = ("T10011", "T10033")


async def check() -> dict:
    report = []
    client = TrafaClient()
    try:
        listed = {
            entry["unique_id"]: entry
            for entry in product_list(
                await client.get_json(STRUCTURE_URL, {"lang": "SV"})
            )
        }
        for product in PRODUCTS:
            item = {"product": product}
            try:
                documents = await fetch_metadata(listed[product], client=client)
                document = documents[0]
                time = (document.get("role", {}).get("time") or [None])[0]
                selection = {
                    key: category_codes(node["category"])[:1]
                    for key, node in document["dimension"].items()
                }
                if time:
                    selection[time] = category_codes(
                        document["dimension"][time]["category"]
                    )[-1:]
                shared = document["extension"]["nordicintel"]
                result = await retrieve(
                    client.session,
                    provider_code="trafa",
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
                validate_retrieval(result, selection=selection)
                item.update(
                    datasets=len(documents),
                    dataset=shared["dataset_code"],
                    selection=selection,
                    metadata_validation="passed",
                    returned_values=len(result["value"]),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {product}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("output/trafa-live.json"))
    args = parser.parse_args()
    result = asyncio.run(check())
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

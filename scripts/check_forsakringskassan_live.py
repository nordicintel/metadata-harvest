"""Opt-in live sample: two Försäkringskassan reports; never a corpus scan.

Fetches ``/reports`` and the texts once, rebuilds and validates each sampled
report's table documents from its ``meta.json``, then retrieves one small filtered
selection of the first table (newest year, totals elsewhere) and validates the
fragment. About eight requests at one per second; no observations are saved.
"""

import argparse
import asyncio
from pathlib import Path

from harvesting.adapters.forsakringskassan.api import REPORTS_URL, TEXTS_URL
from harvesting.adapters.forsakringskassan.client import ForsakringskassanClient
from harvesting.adapters.forsakringskassan.source import catalogue
from harvesting.adapters.forsakringskassan.workflow import (
    check_texts,
    fetch_metadata,
)
from harvesting.storage import write
from harvesting.validation import validate_retrieval
from retrieval._semantics import category_codes
from retrieval.adapters.forsakringskassan import (
    REQUIRED_METADATA,
    retrieve,
)

REPORTS = ("ase-avslutsanledning", "ase-beslut-lan")


def small_selection(document: dict) -> dict:
    """Newest year (listed first), its first two months, ``ALL`` where it exists
    plus one other code, else the first two codes."""
    selection = {}
    for key in document["id"]:
        codes = category_codes(document["dimension"][key]["category"])
        if key == "ar":
            selection[key] = codes[:1]
        elif "ALL" in codes:
            selection[key] = ["ALL", *[c for c in codes if c != "ALL"][:1]]
        else:
            selection[key] = codes[:2]
    return selection


async def check() -> dict:
    report = []
    client = ForsakringskassanClient()
    try:
        listed = {
            item.report: item.record()
            for item in catalogue(await client.get_json(REPORTS_URL), [])[0]
        }
        texts = check_texts(await client.get_json(TEXTS_URL))
        for name in REPORTS:
            item = {"report": name}
            try:
                documents = await fetch_metadata(
                    listed[name], client=client, texts=texts
                )
                document = documents[0]
                shared = document["extension"]["nordicintel"]
                selection = small_selection(document)
                await asyncio.sleep(1.0)  # retrieval bypasses the paced client
                result = await retrieve(
                    client.session,
                    provider_code="forsakringskassan",
                    dataset_code=shared["dataset_code"],
                    language="sv",
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared["metadata_url"],
                    metadata={
                        REQUIRED_METADATA[0]: document["id"],
                        REQUIRED_METADATA[1]: document["extension"][
                            "forsakringskassan"
                        ],
                    },
                )
                validate_retrieval(result, selection=selection)
                values = result["value"]
                item.update(
                    datasets=[
                        d["extension"]["nordicintel"]["dataset_code"] for d in documents
                    ],
                    dataset=shared["dataset_code"],
                    selection=selection,
                    metadata_validation="passed",
                    returned_cells=len(values),
                    returned_suppressed=len(result.get("status") or {}),
                    status="passed" if values else "failed",
                )
                if not values:
                    item["error"] = "no values returned for the sample selection"
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {name}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "harvest_requests": client.request_count,
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/forsakringskassan-live.json")
    )
    args = parser.parse_args()
    result = asyncio.run(check())
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

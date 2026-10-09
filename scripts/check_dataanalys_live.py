"""Opt-in live sample of the analysverktyg; never a corpus scan.

About 12 requests, one per second: the chart and dashboard lists, the GONV
catalogue and column probe, G082 and G013 rebuilt and validated, and one retrieval
each (newest period, Riket, totals) validated as a fragment. Observations are not
saved.
"""

import argparse
import asyncio
from pathlib import Path

from metadata_harvest.adapters.dataanalys.client import DataanalysClient
from metadata_harvest.adapters.dataanalys.retrieval import REQUIRED_METADATA, retrieve
from metadata_harvest.adapters.dataanalys.workflow import discover, fetch_metadata
from metadata_harvest.semantics import category_codes
from metadata_harvest.storage import write
from metadata_harvest.validation import validate_retrieval

AREA = "GONV"
INDICATORS = ("G082", "G013")


def small_selection(document: dict) -> dict:
    """Newest period, Riket and every dimension's total (else its first code)."""
    selection = {}
    for name in document["id"]:
        dimension = document["dimension"][name]
        codes = category_codes(dimension["category"])
        total = dimension.get("extension", {}).get("nordicintel", {})
        if name == "Tid":
            selection[name] = codes[-1:]
        elif total.get("elimination_value"):
            selection[name] = [total["elimination_value"]]
        else:
            selection[name] = codes[:1]
    return selection


async def check() -> dict:
    report: dict = {"verification": "bounded sample only", "documents": []}
    client = DataanalysClient()
    try:
        discovery = await discover(client)
        report["discovery"] = {
            "charts": discovery.charts_count,
            "charts_complete": discovery.charts_complete,
            "dashboards": discovery.dashboards_count,
            "dashboards_complete": discovery.dashboards_complete,
            "areas": sorted(discovery.areas),
            "areas_incomplete": discovery.incomplete,
            "unsupported_datasources": discovery.unsupported,
            "problems": [f"{r}: {e}" for r, e in discovery.problems],
        }
        try:
            documents = await fetch_metadata(
                {"discovery": discovery, "area": AREA, "indicators": list(INDICATORS)},
                client=client,
            )
        except Exception as error:
            documents = []
            report["documents"].append(
                {
                    "area": AREA,
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
        for document in documents:
            shared = document["extension"]["nordicintel"]
            native = document["extension"]["socialstyrelsen_analys"]
            item = {
                "dataset": shared["dataset_code"],
                "label": document["label"],
                "size": dict(zip(document["id"], document["size"], strict=True)),
                "source_url": shared.get("source_url"),
                "link_resolution": native["link_resolution"],
                "updated": document.get("updated"),
                "metadata_validation": "passed",
            }
            try:
                selection = small_selection(document)
                await asyncio.sleep(1.0)  # retrieval uses the session unpaced
                fragment = await retrieve(
                    client.session,
                    provider_code=shared["provider_code"],
                    dataset_code=shared["dataset_code"],
                    language="sv",
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared.get("metadata_url"),
                    metadata={
                        REQUIRED_METADATA[0]: document["id"],
                        REQUIRED_METADATA[1]: native,
                    },
                )
                validate_retrieval(fragment, selection=selection)
                values = fragment["value"]
                item.update(
                    selection=selection,
                    returned_values=len(values),
                    returned_status=len(fragment.get("status") or {}),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {item['dataset']}", flush=True)
            report["documents"].append(item)
    finally:
        await client.close()
    report["requests"] = client.request_count + len(report["documents"])
    report["observations_saved"] = False
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/dataanalys-live.json")
    )
    args = parser.parse_args()
    result = asyncio.run(check())
    write(args.report, result)
    raise SystemExit(
        not result["documents"]
        or any(d["status"] != "passed" for d in result["documents"])
    )

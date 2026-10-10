"""Opt-in live sample of Tillväxtverket open data; never a corpus scan.

Probes the change signal (columns/rowCount and distinct LOAD_DATE) of all six
registered datasets, rebuilds and validates one dataset (its distinct-value
queries, label file and data.europa.eu record), then retrieves one small
filtered selection from it. About 21 requests at one per second. The report
holds counts only; no observations are saved.
"""

import argparse
import asyncio
from pathlib import Path

from apps.harvest.metadata_adapters.tillvaxtverket.client import TillvaxtverketClient
from apps.harvest.metadata_adapters.tillvaxtverket.source import BY_NAME, DATASETS
from apps.harvest.metadata_adapters.tillvaxtverket.workflow import (
    _Dataset,
    fetch_metadata,
)
from apps.harvest.storage import write
from apps.model.validation import validate_retrieval
from retrieval._semantics import category_codes
from retrieval.data_adapters.tillvaxtverket import REQUIRED_METADATA, retrieve

SAMPLE = "GuestNights_Capacity_Revenue_Year"


async def check() -> dict:
    probes, report = [], {}
    client = TillvaxtverketClient()
    try:
        for spec in DATASETS:
            item = {"dataset": spec.dataset_code}
            try:
                probe = await _Dataset(client, spec).probe()
                item.update(
                    status="passed",
                    row_count=probe.row_count,
                    load_dates=list(probe.load_dates),
                    updated=probe.updated,
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: probe {spec.name}", flush=True)
            probes.append(item)

        spec = BY_NAME[SAMPLE]
        item = {"dataset": spec.dataset_code}
        try:
            document = await fetch_metadata(spec, client=client)
            dimensions = document["dimension"]
            periods = category_codes(dimensions["Tid"]["category"])
            selection = {
                "Region": ["riket", "lan:01"],
                "Anlaggningstyp": ["Camping", "Hotell"],
                "Tid": periods[-2:],
                "ContentsCode": ["ANTAL_GASTNATTER", "LOGIINTAKT"],
            }
            shared = document["extension"]["nordicintel"]
            result = await retrieve(
                client.session,
                provider_code="tillvaxtverket",
                dataset_code=shared["dataset_code"],
                language="sv",
                selection=selection,
                data_url=shared["data_url"],
                metadata_url=shared["metadata_url"],
                metadata={
                    REQUIRED_METADATA[0]: document["id"],
                    REQUIRED_METADATA[1]: document["extension"]["tillvaxtverket"],
                },
            )
            validate_retrieval(result, selection=selection)
            values = result["value"]
            if not values:
                raise RuntimeError("no values returned for the sample selection")
            item.update(
                status="passed",
                metadata_validation="passed",
                label=document["label"],
                size=dict(zip(document["id"], document["size"], strict=True)),
                official_statistics=shared.get("official_statistics"),
                selection=selection,
                returned_values=len(values),
                null_values=sum(
                    v is None
                    for v in (values if isinstance(values, list) else values.values())
                ),
                statuses=sorted(set((result.get("status") or {}).values())),
            )
        except Exception as error:
            item.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(f"{item['status']}: rebuild and retrieve {SAMPLE}", flush=True)
        report = item
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "harvest_requests": client.request_count,
        "probes": probes,
        "documents": [report],
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/tillvaxtverket-live.json")
    )
    args = parser.parse_args()
    result = asyncio.run(check())
    write(args.report, result)
    raise SystemExit(
        any(d["status"] != "passed" for d in [*result["probes"], *result["documents"]])
    )

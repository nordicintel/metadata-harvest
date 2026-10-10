"""Opt-in live sample of Nationella emissionsdatabasen; never a corpus scan.

Reads ``getfilteroptions/`` once, rebuilds and validates two substances' documents
(one national probe each), then retrieves one small selection: Stockholm kommun,
one Transporter subsector, the newest year. Four requests at one per second; no
observations are saved.
"""

import argparse
import asyncio
from pathlib import Path

from harvesting.adapters.smhi_emissions.api import FILTER_OPTIONS_URL
from harvesting.adapters.smhi_emissions.client import SmhiClient
from harvesting.adapters.smhi_emissions.retrieval import (
    REQUIRED_METADATA,
    retrieve,
)
from harvesting.adapters.smhi_emissions.source import parse_filter_options
from harvesting.adapters.smhi_emissions.workflow import build_substance
from harvesting.semantics import category_codes
from harvesting.storage import write
from harvesting.validation import validate_retrieval

SUBSTANCES = ("CO2", "NOx")
REGION, SECTOR = "0180", "4.1"


async def check() -> dict:
    report = []
    client = SmhiClient()
    try:
        options = parse_filter_options(await client.get_json(FILTER_OPTIONS_URL))
        listing = {
            "substances": len(options.substances),
            "sectors": len(options.sectors),
            "subsectors": sum(len(s.subsectors) for s in options.sectors),
            "complete": options.complete,
            "problems": [f"{r}: {e}" for r, e in options.problems],
            "quarantined": options.quarantined,
        }
        for number, code in enumerate(SUBSTANCES):
            item = {"substance": code}
            try:
                document, submission = await build_substance(
                    client, options.substances[code], options
                )
                shared = document["extension"]["nordicintel"]
                item.update(
                    dataset=shared["dataset_code"],
                    submission=submission,
                    years=category_codes(document["dimension"]["Tid"]["category"]),
                    size=document["size"],
                    metadata_validation="passed",
                )
                if number == 0:  # one bounded retrieval
                    selection = {
                        "Region": [REGION],
                        "Sektor": [SECTOR],
                        "Tid": item["years"][-1:],
                        "ContentsCode": [code],
                    }
                    await asyncio.sleep(1)  # the client's pace, for the caller session
                    result = await retrieve(
                        client.session,
                        provider_code="smhi",
                        dataset_code=shared["dataset_code"],
                        language="sv",
                        selection=selection,
                        data_url=shared["data_url"],
                        metadata_url=shared["metadata_url"],
                        metadata={
                            REQUIRED_METADATA[0]: document["id"],
                            REQUIRED_METADATA[1]: document["extension"][
                                "smhi_emissions"
                            ],
                        },
                    )
                    validate_retrieval(result, selection=selection)
                    item.update(
                        selection=selection,
                        returned_values=len(result["value"]),
                        retrieval_validation="passed",
                    )
                item["status"] = "passed"
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {code}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "requests": client.request_count + 1,
        "listing": listing,
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/smhi_emissions-live.json")
    )
    args = parser.parse_args()
    result = asyncio.run(check())
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

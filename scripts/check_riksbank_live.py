"""Opt-in live sample of the Riksbank APIs; never a corpus scan.

About eleven requests, paced at the provider's 15 s interval (about three minutes):
SWEA ``/Groups`` and ``/Series`` with group 2's latest observations, the forecast
catalogue and policy rounds with one series' latest vintage, and the latest SWESTR
fixing. Each of the three documents is built and validated, then one small recent
selection is retrieved from it. Observations are counted, never saved.
"""

import argparse
import asyncio
from pathlib import Path

from metadata_harvest.adapters.riksbank.client import RiksbankClient
from metadata_harvest.adapters.riksbank.retrieval import REQUIRED_METADATA, retrieve
from metadata_harvest.adapters.riksbank.workflow import build_record, discover_api
from metadata_harvest.semantics import category_codes
from metadata_harvest.storage import read, write
from metadata_harvest.validation import validate_retrieval

SAMPLES = {"swea": "swea_group_2", "mpd": "mpd_SEQGDPNAYSA", "swestr": "swestr"}


def recent_selection(document: dict) -> dict:
    """The first metric category and the latest few periods (and newest round)."""
    selection = {}
    for name in document["id"]:
        codes = category_codes(document["dimension"][name]["category"])
        if name == "Time":
            selection[name] = codes[-7:]
        elif name == "PolicyRound":
            selection[name] = codes[-1:]
        elif name == "Measure":
            selection[name] = ["rate", "volume"]
        else:
            selection[name] = codes[:1]
    return selection


async def check(provider: dict) -> dict:
    report = []
    client = RiksbankClient(provider)
    try:
        for api, code in SAMPLES.items():
            item = {"api": api, "dataset": code}
            try:
                listing = await discover_api(client, api)
                record = next(r for r in listing.records if r["dataset_code"] == code)
                document = await build_record(client, record)
                selection = recent_selection(document)
                shared = document["extension"]["nordicintel"]
                await client.pace()  # retrieval itself never paces
                result = await retrieve(
                    client.session,
                    provider_code="riksbank",
                    dataset_code=code,
                    language="en",
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared["metadata_url"],
                    metadata={
                        REQUIRED_METADATA[0]: document["id"],
                        REQUIRED_METADATA[1]: document["extension"]["riksbank"],
                    },
                )
                validate_retrieval(result, selection=selection)
                item.update(
                    listed=len(listing.records),
                    listing_problems=[str(p[0]) for p in listing.problems],
                    size=document["size"],
                    first_period=shared.get("first_period"),
                    last_period=shared.get("last_period"),
                    selection=selection,
                    metadata_validation="passed",
                    returned_values=len(result["value"]),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {api} {code}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "requests": client.request_count,
        "subscription_key": client.keyed,
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers", type=Path, default=Path("providers.riksbank.json")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("output/riksbank-live.json")
    )
    args = parser.parse_args()
    result = asyncio.run(check(read(args.providers)[0]))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

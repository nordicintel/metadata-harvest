"""Opt-in live sample of Tillväxtverket open data; never a corpus scan.

Runs the adapter for one registered dataset into a temporary directory (its change
signal, distinct-value queries and label file), parses the stored files into a
document (validated), then retrieves one small filtered selection from it. About
ten requests at one per second; no observations are saved.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.tillvaxtverket import parse
from apps.harvest.metadata_adapters.tillvaxtverket.client import TillvaxtverketClient
from apps.harvest.metadata_adapters.tillvaxtverket.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.tillvaxtverket import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

SAMPLE = "GuestNights_Capacity_Revenue_Year"


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = TillvaxtverketClient()
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"products": [SAMPLE]},
            client=client,
        )
        store = SourceStore(root / "out/source", ADAPTER.name)
        state = lifecycle.load_state(root / "out", ADAPTER.collection)
        report = []
        for key, document in sorted(parse.documents(store, state, [binding])):
            item = {"dataset": key}
            try:
                if isinstance(document, Exception):
                    raise document
                validate_metadata(document, adapter=ADAPTER.name)
                periods = category_codes(document["dimension"]["Tid"]["category"])
                selection = {
                    "Region": ["riket", "lan:01"],
                    "Anlaggningstyp": ["Camping", "Hotell"],
                    "Tid": periods[-2:],
                    "ContentsCode": ["ANTAL_GASTNATTER", "LOGIINTAKT"],
                }
                shared = document["extension"]["nordicintel"]
                fragment = await retrieve(
                    client.session,
                    provider_code=shared["provider_code"],
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
                validate_retrieval(fragment, selection=selection)
                values = fragment["value"]
                if not values:
                    raise RuntimeError("no values returned for the sample selection")
                item.update(
                    status="passed",
                    metadata_validation="passed",
                    label=document["label"],
                    size=dict(zip(document["id"], document["size"], strict=True)),
                    selection=selection,
                    returned_values=len(values),
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {key}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "harvest_requests": client.request_count,
        "run": {k: result[k] for k in ("status", "counts", "errors")},
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/tillvaxtverket-live.json")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tillvaxtverket-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

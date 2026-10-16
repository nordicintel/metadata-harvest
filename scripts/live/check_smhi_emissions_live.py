"""Opt-in live sample of Nationella emissionsdatabasen; never a corpus scan.

Runs the adapter for two substances into a temporary directory (the tree and one
national probe each), parses the stored files into documents, then retrieves one
small selection: Stockholm kommun, one Transporter subsector, the newest year.
Four requests at one per second; no observations are saved.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.smhi_emissions import parse
from apps.harvest.metadata_adapters.smhi_emissions.client import SmhiClient
from apps.harvest.metadata_adapters.smhi_emissions.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.smhi_emissions import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

SUBSTANCES = ("CO2", "NOx")
REGION, SECTOR = "0180", "4.1"


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = SmhiClient()
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"subjects": list(SUBSTANCES)},
            client=client,
        )
        store = SourceStore(root / "out/source", ADAPTER.name)
        state = lifecycle.load_state(root / "out", ADAPTER.collection)
        report = []
        for number, (key, document) in enumerate(
            sorted(parse.documents(store, state, [binding]))
        ):
            item = {"key": key}
            try:
                if isinstance(document, Exception):
                    raise document
                validate_metadata(document, adapter=ADAPTER.name)
                shared = document["extension"]["nordicintel"]
                item.update(
                    years=category_codes(document["dimension"]["Tid"]["category"]),
                    size=document["size"],
                    metadata_validation="passed",
                )
                if number == 0:  # one bounded retrieval
                    selection = {
                        "Region": [REGION],
                        "Sektor": [SECTOR],
                        "Tid": item["years"][-1:],
                        "ContentsCode": [shared["dataset_code"].split("_", 1)[1]],
                    }
                    await asyncio.sleep(1)  # the client's pace, for the caller session
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
                            REQUIRED_METADATA[1]: document["extension"][
                                "smhi_emissions"
                            ],
                        },
                    )
                    validate_retrieval(fragment, selection=selection)
                    item.update(
                        selection=selection,
                        returned_values=len(fragment["value"]),
                        retrieval_validation="passed",
                    )
                item["status"] = "passed"
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {key}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "requests": client.request_count + 1,
        "run": {k: result[k] for k in ("status", "counts", "errors")},
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/smhi_emissions-live.json")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="smhi-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

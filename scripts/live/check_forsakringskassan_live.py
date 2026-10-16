"""Opt-in live sample: two Försäkringskassan reports; never a corpus scan.

Runs the adapter for the sampled reports into a temporary directory (``/reports``,
the texts, each ``meta.json`` and the undeclared-column probes), parses the stored
files into documents (validating them), then retrieves one small filtered
selection of each report's first table (newest year, totals elsewhere) and
validates the fragment. About eight requests at one per second.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.forsakringskassan import parse
from apps.harvest.metadata_adapters.forsakringskassan.client import (
    ForsakringskassanClient,
)
from apps.harvest.metadata_adapters.forsakringskassan.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.forsakringskassan import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

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


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = ForsakringskassanClient()
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"subjects": list(REPORTS), "full": True},
            client=client,
        )
        store = SourceStore(root / "out/source", ADAPTER.name)
        state = lifecycle.load_state(root / "out", ADAPTER.collection)
        report, retrieved = [], set()
        for key, document in sorted(parse.documents(store, state, [binding])):
            item = {"dataset": key}
            try:
                if isinstance(document, Exception):
                    raise document
                validate_metadata(document, adapter=ADAPTER.name)
                item["metadata_validation"] = "passed"
                name = key.split("/")[1]
                if name not in retrieved:  # one retrieval per report
                    retrieved.add(name)
                    shared = document["extension"]["nordicintel"]
                    selection = small_selection(document)
                    await asyncio.sleep(1.0)  # retrieval bypasses the paced client
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
                                "forsakringskassan"
                            ],
                        },
                    )
                    validate_retrieval(fragment, selection=selection)
                    values = fragment["value"]
                    item.update(
                        selection=selection,
                        returned_cells=len(values),
                        returned_suppressed=len(fragment.get("status") or {}),
                    )
                    if not values:
                        raise RuntimeError("no values returned for the sample")
                item["status"] = "passed"
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
        "--report", type=Path, default=Path("output/forsakringskassan-live.json")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="forsakringskassan-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

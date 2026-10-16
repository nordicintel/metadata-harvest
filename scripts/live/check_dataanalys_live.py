"""Opt-in live sample of the analysverktyg; never a corpus scan.

Runs the adapter for two GONV indicators into a temporary directory (the chart
and dashboard lists, the GONV catalogue and column probe, two cube queries),
parses the stored files into documents (validated), then retrieves one small
selection each (newest period, Riket, totals) validated as a fragment. About 12
requests, one per second; observations are not saved.
"""

import argparse
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.dataanalys import parse
from apps.harvest.metadata_adapters.dataanalys.client import DataanalysClient
from apps.harvest.metadata_adapters.dataanalys.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.dataanalys import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

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


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = DataanalysClient(binding.options)
    report: dict = {
        "verification": "bounded sample only",
        "checked_at": datetime.now(UTC).isoformat(),
        "documents": [],
    }
    retrieval_requests = 0
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"areas": [AREA], "indicators": list(INDICATORS)},
            client=client,
        )
        report["run"] = {
            k: result[k]
            for k in ("status", "counts", "errors", "areas", "areas_incomplete")
        }
        store = SourceStore(root / "out/source", ADAPTER.name)
        state = lifecycle.load_state(root / "out", ADAPTER.collection)
        for key, document in sorted(parse.documents(store, state, [binding])):
            item = {"dataset": key}
            try:
                if isinstance(document, Exception):
                    raise document
                validate_metadata(document, adapter=ADAPTER.name)
                shared = document["extension"]["nordicintel"]
                native = document["extension"]["socialstyrelsen_analys"]
                item.update(
                    label=document["label"],
                    size=dict(zip(document["id"], document["size"], strict=True)),
                    link_resolution=native["link_resolution"],
                    metadata_validation="passed",
                )
                selection = small_selection(document)
                await asyncio.sleep(1.0)  # retrieval uses the session unpaced
                retrieval_requests += 1
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
                item.update(
                    selection=selection,
                    returned_values=len(fragment["value"]),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {key}", flush=True)
            report["documents"].append(item)
    finally:
        await client.close()
    report["requests"] = client.request_count + retrieval_requests
    report["observations_saved"] = False
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/dataanalys-live.json")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="dataanalys-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(
        not result["documents"]
        or result["run"]["status"] != "success"
        or any(d["status"] != "passed" for d in result["documents"])
    )

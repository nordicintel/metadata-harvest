"""Opt-in live sample: three SDB subject documents; never a corpus scan.

Runs the adapter for the sampled subjects into a temporary directory, parses the
stored files into documents (validating them), then retrieves one small selection
per document using each dimension's first category and checks the fragment.
"""

import argparse
import asyncio
import tempfile
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.sdb import parse
from apps.harvest.metadata_adapters.sdb.client import SdbClient
from apps.harvest.metadata_adapters.sdb.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.sdb import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

SUBJECTS = ("amning", "dodsorsaker")


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = SdbClient(binding.options, base_url=binding.endpoint_url)
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"subjects": list(SUBJECTS)},
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
                selection = {
                    name: category_codes(node["category"])[:1]
                    for name, node in document["dimension"].items()
                }
                if "ar" in selection:
                    selection["ar"] = category_codes(
                        document["dimension"]["ar"]["category"]
                    )[-1:]
                shared = document["extension"]["nordicintel"]
                fragment = await retrieve(
                    client.session,
                    provider_code=shared["provider_code"],
                    dataset_code=shared["dataset_code"],
                    language=shared["language"],
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared["metadata_url"],
                    metadata={
                        REQUIRED_METADATA[0]: document["extension"]
                        .get("socialstyrelsen", {})
                        .get("result_dimensions", {})
                    },
                )
                validate_retrieval(fragment, selection=selection)
                item.update(
                    selection=selection,
                    metadata_validation="passed",
                    returned_values=len(fragment["value"]),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {key}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "run": {k: result[k] for k in ("status", "counts", "errors")},
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("output/sdb-live.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sdb-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

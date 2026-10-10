"""Opt-in live sample: three SDB subject documents; never a corpus scan.

Rebuilds each sampled document from SDB (validating it), then retrieves one small
selection using each dimension's first category and checks the fragment.
"""

import argparse
import asyncio
from pathlib import Path

from metadata_harvest.adapters.sdb.client import SdbClient
from metadata_harvest.adapters.sdb.mapping import time_unit_from_swedish_label
from metadata_harvest.adapters.sdb.retrieval import REQUIRED_METADATA, retrieve
from metadata_harvest.adapters.sdb.workflow import fetch_metadata
from metadata_harvest.configuration import providers_for_adapter
from metadata_harvest.semantics import category_codes
from metadata_harvest.storage import write
from metadata_harvest.validation import validate_retrieval

SAMPLE = (("sv", "amning"), ("sv", "dodsorsaker"), ("en", "dodsorsaker"))


async def check(provider: dict) -> dict:
    report = []
    client = SdbClient(provider)
    try:
        listings = {
            language: {row["namn"]: row for row in await client.get(language)}
            for language in {language for language, _ in SAMPLE}
        }
        for language, code in SAMPLE:
            item = {"dataset": f"{language}/{code}"}
            try:
                row = listings[language][code]
                swedish = listings["sv"][code]["text"]
                document = await fetch_metadata(
                    {
                        "language": language,
                        "namn": code,
                        "text": row["text"],
                        "time_unit": time_unit_from_swedish_label(swedish),
                    },
                    client=client,
                )
                selection = {
                    key: category_codes(node["category"])[:1]
                    for key, node in document["dimension"].items()
                }
                if "ar" in selection:
                    selection["ar"] = category_codes(
                        document["dimension"]["ar"]["category"]
                    )[-1:]
                shared = document["extension"]["nordicintel"]
                result = await retrieve(
                    client.session,
                    provider_code="socialstyrelsen",
                    dataset_code=code,
                    language=language,
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared["metadata_url"],
                    metadata={
                        REQUIRED_METADATA[0]: document["extension"]
                        .get("socialstyrelsen", {})
                        .get("result_dimensions", {})
                    },
                )
                validate_retrieval(result, selection=selection)
                item.update(
                    selection=selection,
                    metadata_validation="passed",
                    returned_values=len(result["value"]),
                    status="passed",
                )
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(f"{item['status']}: {language}/{code}", flush=True)
            report.append(item)
    finally:
        await client.close()
    return {
        "verification": "bounded sample only",
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=Path("output/sdb-live.json"))
    args = parser.parse_args()
    result = asyncio.run(
        check(providers_for_adapter("sdb", legacy_path=args.providers)[0])
    )
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

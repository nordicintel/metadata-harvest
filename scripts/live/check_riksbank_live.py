"""Opt-in live sample of the Riksbank APIs; never a corpus scan.

Runs the adapter for one dataset of each API into a temporary directory, paced
at the provider's 15 s interval (about three minutes): the catalogues plus one
probe each. Each document is parsed and validated, then one small recent
selection is retrieved from it. Observations are counted, never saved.
"""

import argparse
import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import registry
from apps.harvest.metadata_adapters.riksbank import parse
from apps.harvest.metadata_adapters.riksbank.client import RiksbankClient
from apps.harvest.metadata_adapters.riksbank.harvest import ADAPTER
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import write
from harvest import lifecycle
from retrieval._semantics import category_codes
from retrieval.data_adapters.riksbank import REQUIRED_METADATA, retrieve
from schema.validation import validate_metadata, validate_retrieval

SAMPLES = ("swea_group_2", "mpd_SEQGDPNAYSA", "swestr")


class PacedRetrievalSession:
    """Count and pace every request in the sample's caller-owned session."""

    def __init__(self, client):
        self.client = client

    def get(self, *args, **kwargs):
        return PacedRequest(self.client, args, kwargs)


class PacedRequest:
    def __init__(self, client, args, kwargs):
        self.client, self.args, self.kwargs = client, args, kwargs

    async def __aenter__(self):
        await self.client.pace()
        self.client.request_count += 1
        self.context = self.client.session.get(*self.args, **self.kwargs)
        return await self.context.__aenter__()

    async def __aexit__(self, *args):
        return await self.context.__aexit__(*args)


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


async def check(root: Path) -> dict:
    binding = registry.load().bindings(ADAPTER.name)[0]
    client = RiksbankClient(binding.options)
    try:
        result = await lifecycle.run(
            ADAPTER,
            binding,
            root / "data",
            root / "out",
            selection={"subjects": list(SAMPLES)},
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
                selection = recent_selection(document)
                shared = document["extension"]["nordicintel"]
                fragment = await retrieve(
                    PacedRetrievalSession(client),
                    provider_code=shared["provider_code"],
                    dataset_code=shared["dataset_code"],
                    language="en",
                    selection=selection,
                    data_url=shared["data_url"],
                    metadata_url=shared["metadata_url"],
                    metadata={
                        REQUIRED_METADATA[0]: document["id"],
                        REQUIRED_METADATA[1]: document["extension"]["riksbank"],
                    },
                )
                validate_retrieval(fragment, selection=selection)
                item.update(
                    size=document["size"],
                    first_period=shared.get("first_period"),
                    last_period=shared.get("last_period"),
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
        "checked_at": datetime.now(UTC).isoformat(),
        "verification": "bounded sample only",
        "requests": client.request_count,
        "subscription_key": client.keyed,
        "run": {k: result[k] for k in ("status", "counts", "errors")},
        "documents": report,
        "observations_saved": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("output/riksbank-live.json")
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="riksbank-live-") as temporary:
        result = asyncio.run(check(Path(temporary)))
    write(args.report, result)
    raise SystemExit(any(d["status"] != "passed" for d in result["documents"]))

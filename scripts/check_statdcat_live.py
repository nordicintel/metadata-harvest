"""Bounded live equivalence checks for direct adapter and catalog retrieval interfaces."""

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from harvesting.accepted import load_contributions, read_records
from harvesting.models import Dataset
from harvesting.statdcat import convert_dataset, legacy_document
from harvesting.validation import validate_retrieval
from retrieval import retrieve_distribution


class RecordedSession:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.session, name)

    def get(self, url, **kwargs):
        self.calls.append(("GET", str(url), kwargs))
        return self.session.get(url, **kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", str(url), kwargs))
        return self.session.post(url, **kwargs)


def source(adapter, provider, scope=None):
    for record in load_contributions(Path("data") / adapter / "metadata"):
        shared = record["extension"]["nordicintel"]
        if shared["provider_code"] != provider or shared["language"] != "sv":
            continue
        if scope is None or record["extension"]["kolada"]["scope"] == scope:
            return record
    raise ValueError("No representative source record")


def accepted_source(adapter, provider, scope=None):
    """Exercise the saved distribution directly after the corpus cutover."""
    for path in sorted((Path("data") / adapter / "metadata").glob("*.jsonl*")):
        for document in read_records(path):
            if document.get("@type") != "dcat:Dataset":
                continue
            dataset = Dataset.from_jsonld(document)
            for distribution in dataset.distributions:
                binding = distribution.retrieval
                if (
                    binding is not None
                    and binding.source.provider_code == provider
                    and binding.source.language == "sv"
                    and (scope is None or binding.options.scope == scope)
                ):
                    return legacy_document(distribution, dataset), dataset, distribution
    old = source(adapter, provider, scope)
    dataset = Dataset.from_jsonld(
        convert_dataset(old, adapter=adapter).dataset.to_jsonld()
    )
    return old, dataset, dataset.distributions[0]


async def main():
    report = {"kind": "live", "started_at": datetime.now(UTC).isoformat(), "checks": []}
    for adapter, provider, scope in [
        ("pxweb_v2", "scb", None),
        ("pxweb_v1", "csn", None),
        ("kolada", "kolada", "municipality"),
        ("kolada", "kolada", "ou"),
        ("entrysoft", "catalogalese", None),
    ]:
        check = {"adapter": adapter, "scope": scope}
        try:
            old, dataset, distribution = accepted_source(adapter, provider, scope)
            selection = {
                axis.code: axis.categories[-2:-1]
                if axis.code in {"year", "Tid"} and len(axis.categories) > 1
                else axis.categories[:1]
                for axis in distribution.retrieval.dimensions
            }
            mod = importlib.import_module(f"retrieval.adapters.{adapter}")
            metadata = {}
            for pointer in mod.REQUIRED_METADATA:
                value = old
                for part in pointer.strip("/").split("/"):
                    value = value[part.replace("~1", "/").replace("~0", "~")]
                metadata[pointer] = value
            shared = old["extension"]["nordicintel"]
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90)
            ) as connection:
                session = RecordedSession(connection)
                previous = await mod.retrieve(
                    session,
                    provider_code=shared["provider_code"],
                    dataset_code=shared["dataset_code"],
                    language=shared["language"],
                    data_url=shared.get("data_url"),
                    metadata_url=shared.get("metadata_url"),
                    selection=selection,
                    metadata=metadata,
                )
                requests = list(session.calls)
                session.calls.clear()
                await asyncio.sleep(1)
                current = await retrieve_distribution(
                    session, distribution.to_jsonld(), selection=selection
                )
                assert requests == session.calls, "Different HTTP requests"
                assert current == previous, "Different observation results"
                validate_retrieval(current, selection=selection)
                assert not connection.closed
            check.update(
                status="passed",
                dataset=dataset.id,
                requests=len(requests),
                size=current["size"],
                observation_values=len(current["value"]),
            )
        except Exception as error:
            check.update(status="failed", error=f"{type(error).__name__}: {error}")
        report["checks"].append(check)
        print(json.dumps(check), flush=True)
        target = Path("output/statdcat-live.json")
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if any(check["status"] != "passed" for check in report["checks"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

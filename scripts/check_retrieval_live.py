"""Bounded live metadata-to-retrieval acceptance; never persists observations."""

import asyncio
import importlib
import json
import time
from pathlib import Path

import aiohttp

from harvesting.adapters.pxweb_v1.discovery import discover_provider
from harvesting.adapters.pxweb_v1.metadata import get_pxweb_metadata
from harvesting.adapters.pxweb_v2.client import Client
from harvesting.adapters.pxweb_v2.metadata import fetch_metadata
from harvesting.configuration import providers_for_adapter
from harvesting.storage import JsonlStore, now, write
from harvesting.validation import validate_metadata, validate_retrieval
from retrieval._semantics import category_codes


async def retrieve(document, adapter, interval):
    module = importlib.import_module(f"retrieval.adapters.{adapter}")
    identity = document["extension"]["nordicintel"]
    metadata = {
        "/dimension": document["dimension"],
        f"/extension/{adapter}": document["extension"][adapter],
    }
    selection = {
        code: category_codes(document["dimension"][code]["category"])[:1]
        for code in document["id"]
    }
    trace = aiohttp.TraceConfig()
    next_request = 0.0

    async def pace(session, context, params):
        nonlocal next_request
        await asyncio.sleep(max(0, next_request - time.monotonic()))
        next_request = time.monotonic() + interval

    trace.on_request_start.append(pace)
    # Pacing belongs to this caller, never to the retrieval package.
    await asyncio.sleep(interval)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=90), trace_configs=[trace]
    ) as session:
        fragment = await module.retrieve(
            session,
            provider_code=identity["provider_code"],
            dataset_code=identity["dataset_code"],
            language=identity["language"],
            selection=selection,
            data_url=identity["data_url"],
            metadata_url=identity["metadata_url"],
            metadata=metadata,
        )
    validate_retrieval(fragment, selection=selection)
    return {
        "dimensions": fragment["id"],
        "size": fragment["size"],
        "has_status": "status" in fragment,
    }


async def run(output):
    report = {"started_at": now(), "bounded": True, "checks": []}
    listings = list(JsonlStore("data/pxweb_v1/table-records").records.values())
    semaphore = asyncio.Semaphore(3)

    async def v1(provider):
        async with semaphore:
            item = {"adapter": "pxweb_v1", "provider_code": provider["code"]}
            report["checks"].append(item)
            try:
                async with asyncio.timeout(900):
                    rows = [
                        r for r in listings if r["provider_code"] == provider["code"]
                    ]
                    if not rows:
                        async for scope in discover_provider(provider):
                            if scope["records"]:
                                rows = scope["records"]
                                break
                    if not rows:
                        raise ValueError(
                            "No discovered table available for bounded check"
                        )
                    row = rows[0]
                    item.update(dataset_code=row["id"], language=row["language"])
                    document = await get_pxweb_metadata(
                        row, provider_code=provider["code"]
                    )
                    validate_metadata(document, adapter="pxweb_v1")
                    item.update(
                        await retrieve(
                            document, "pxweb_v1", max(1, provider.get("rate_limit", 1))
                        )
                    )
                    item["status"] = "passed"
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(json.dumps(item, ensure_ascii=False), flush=True)
            write(output, report)

    await asyncio.gather(*(v1(p) for p in providers_for_adapter("pxweb_v1")))
    provider = providers_for_adapter("pxweb_v2")[0]
    v2_rows = list(JsonlStore("data/pxweb_v2/table-records").records.values())
    async with Client(provider["base_api_url"]) as client:
        client.configure(await client.get("config"))
        for language in provider["languages"]:
            item = {"adapter": "pxweb_v2", "provider_code": "scb", "language": language}
            report["checks"].append(item)
            try:
                row = next(r for r in v2_rows if r["language"] == language)
                item["dataset_code"] = row["dataset_code"]
                document = await fetch_metadata(row, client=client)
                validate_metadata(document, adapter="pxweb_v2")
                item.update(await retrieve(document, "pxweb_v2", client.interval))
                item["status"] = "passed"
            except Exception as error:
                item.update(status="failed", error=f"{type(error).__name__}: {error}")
            print(json.dumps(item, ensure_ascii=False), flush=True)
            write(output, report)
    report["finished_at"] = now()
    report["passed"] = all(c["status"] == "passed" for c in report["checks"])
    write(output, report)
    return report


if __name__ == "__main__":
    report = asyncio.run(run(Path("output/retrieval-live/report.json")))
    raise SystemExit(0 if report["passed"] else 1)

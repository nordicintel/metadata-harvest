"""Explicit full-table live acceptance; library code never writes these files."""

import argparse
import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

from harvesting import get_pxweb_metadata, get_pxweb_tables
from harvesting.configuration import providers_for_adapter
from harvesting.mapping import source_record
from harvesting.validation import validate_metadata


class RequestAudit(logging.Handler):
    def __init__(self):
        super().__init__()
        self.requests = []

    def emit(self, record):
        if hasattr(record, "metadata_request"):
            self.requests.append(record.metadata_request)


def now():
    return datetime.now(UTC).isoformat()


async def run(provider, output, *, limit=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "provider": provider["code"],
        "started_at": now(),
        "limited": limit is not None,
        "implementation_sha256": hashlib.sha256(
            Path("harvesting/adapters/pxweb_v1/metadata.py").read_bytes()
        ).hexdigest(),
        "combinations": [],
    }
    audit = RequestAudit()
    logger = logging.getLogger("harvesting.adapters.pxweb_v1.metadata")
    logger.setLevel(logging.INFO)
    logger.addHandler(audit)

    def save():
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()
    try:
        for language in provider["languages"]:
            for database in provider["database_ids"]:
                combination = {
                    "language": language,
                    "database_id": unquote(database),
                    "started_at": now(),
                    "tables": [],
                    "status": "running",
                }
                report["combinations"].append(combination)
                try:
                    url = (
                        provider["base_web_url"].rstrip("/")
                        + "/"
                        + language
                        + "/"
                        + quote(unquote(database), safe="")
                        + "/"
                    )
                    async with asyncio.timeout(1800):
                        tables = await get_pxweb_tables(url)
                    combination["web_count"] = len(tables)
                    for table in tables if limit is None else tables[:limit]:
                        item = {
                            "id": table["id"],
                            "path": table["path"],
                            "api_url": table["api_url"],
                            "started_at": now(),
                        }
                        combination["tables"].append(item)
                        audit.requests = []
                        try:
                            async with asyncio.timeout(900):
                                dataset = await get_pxweb_metadata(
                                    table, provider_code=provider["code"]
                                )
                            assert dataset["value"] == []
                            validate_metadata(dataset, adapter="pxweb_v1")
                            identity = source_record(dataset)
                            assert all(
                                identity[key] == value for key, value in table.items()
                            )
                            item.update(
                                status="success",
                                size=dataset["size"],
                                dimensions=dataset["id"],
                                covered_categories=sum(dataset["size"]),
                                category_coverage_complete=True,
                                published=identity["published"],
                                sha256=hashlib.sha256(
                                    json.dumps(
                                        dataset, ensure_ascii=False, sort_keys=True
                                    ).encode()
                                ).hexdigest(),
                            )
                        except Exception as error:
                            item.update(
                                status="failed",
                                error=f"{type(error).__name__}: {error}",
                            )
                        item.update(
                            finished_at=now(),
                            requests=len(audit.requests),
                            get_requests=sum(
                                r["method"] == "GET" for r in audit.requests
                            ),
                            post_requests=sum(
                                r["method"] == "POST" for r in audit.requests
                            ),
                        )
                        print(
                            f"{provider['code']} {language}/{database} {table['id']}: {item['status']} ({len(combination['tables'])}/{len(tables)})",
                            flush=True,
                        )
                        save()
                        await asyncio.sleep(provider.get("rate_limit", 1))
                    combination["status"] = (
                        "success"
                        if all(t["status"] == "success" for t in combination["tables"])
                        else "failed"
                    )
                except Exception as error:
                    combination.update(
                        status="failed", error=f"{type(error).__name__}: {error}"
                    )
                combination["finished_at"] = now()
                save()
    finally:
        logger.removeHandler(audit)
        report["finished_at"] = now()
        report["accepted"] = (
            limit is None
            and len(report["combinations"])
            == len(provider["languages"]) * len(provider["database_ids"])
            and all(
                c["status"] == "success" and len(c["tables"]) == c["web_count"]
                for c in report["combinations"]
            )
        )
        save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--limit", type=int, help="Smoke test only; never counts as full acceptance"
    )
    args = parser.parse_args()
    providers = providers_for_adapter("pxweb_v1")
    provider = next((p for p in providers if p["code"] == args.provider), None)
    if provider is None or (args.limit is not None and args.limit < 1):
        parser.error("Unknown provider or invalid limit")
    result = asyncio.run(run(provider, args.output, limit=args.limit))
    raise SystemExit(0 if result["accepted"] else 1)

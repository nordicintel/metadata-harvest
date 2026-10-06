"""Explicit live compatibility run; writes evidence outside the library."""

import argparse
import asyncio
import hashlib
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from uuid import UUID

from playwright.async_api import async_playwright

import metadata_harvest.pxweb as implementation
from metadata_harvest import get_pxweb_tables

_audit = ContextVar("audit", default=None)


def comparable(records):
    """Ignore only observed UUID-valued rxid session parameters in comparisons."""
    result = []
    for record in records:
        parsed = urlsplit(record["web_url"])
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == "rxid":
                try:
                    UUID(value)
                    continue
                except ValueError:
                    pass
            query.append((key, value))
        result.append(
            {**record, "web_url": urlunsplit(parsed._replace(query=urlencode(query)))}
        )
    return sorted(
        result, key=lambda x: (x["language"], x["database_id"], x["path"], x["id"])
    )


def install_audit():
    original_context = implementation._context
    original_complete = implementation._complete_harvest

    async def context(browser, insecure=False):
        result = await original_context(browser, insecure)
        audit = _audit.get()

        def response_received(response):
            if implementation._API.search(unquote(urlsplit(response.url).path)):
                audit["api_responses"].append(response.url)

        result.on("response", response_received)
        return result

    async def complete(page, root, language, database):
        records = await original_complete(page, root, language, database)
        _audit.get()["confirmed_databases"].append(database)
        state = page.context._pxweb_api
        _audit.get()["enrichment"].append(
            {
                "language": language,
                "database_id": database,
                "flat_requests": int(state.get("attempted", False)),
                "folder_requests": state.get("folder_request_count", 0),
                "folder_count": len(state.get("folders", {})),
                "omitted": state.get("omitted", []),
            }
        )
        return records

    implementation._context = context
    implementation._complete_harvest = complete


async def checked_call(url, languages, databases=None, *, timeout=900):
    audit = {"api_responses": [], "confirmed_databases": [], "enrichment": []}
    token = _audit.set(audit)
    try:
        async with asyncio.timeout(timeout):
            rows = await get_pxweb_tables(url, languages, databases)
        if audit["api_responses"]:
            raise AssertionError(
                f"API response reached browser: {audit['api_responses']}"
            )
        return rows, audit
    except Exception as error:
        error.pxweb_audit = audit
        raise
    finally:
        _audit.reset(token)


async def verify_sample(record):
    """Independently navigate one emitted table and retain rendered evidence."""
    audit = {"api_responses": [], "confirmed_databases": []}
    token = _audit.set(audit)
    try:
        async with asyncio.timeout(45), async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:

                async def inspect(page):
                    await implementation._goto(page, record["web_url"])
                    body = " ".join((await page.locator("body").inner_text()).split())
                    actual = implementation._parse_url(page.url)
                    expected = implementation._parse_url(record["web_url"])
                    panel_date = await page.locator(
                        ".information_lastupdated_value"
                    ).all_text_contents()
                    evidence = {
                        "id": record["id"],
                        "title_found": record["title"] in body,
                        "category_names_found": all(
                            x["name"] in body for x in record["category_path"]
                        ),
                        "table_route_matches": (
                            actual.language,
                            actual.database,
                            actual.tail,
                        )
                        == (expected.language, expected.database, expected.tail),
                        "listing_date": record["published"],
                        "table_panel_dates": [x.strip() for x in panel_date],
                        "api_response_count": len(audit["api_responses"]),
                    }
                    if not evidence["title_found"]:
                        # The listing is authoritative; table-page wording can differ.
                        parsed = urlsplit(record["web_url"])
                        parent_path = parsed.path.rstrip("/").rsplit("/", 1)[0] + "/"
                        parent_url = urlunsplit(
                            parsed._replace(path=parent_path, query="", fragment="")
                        )
                        await implementation._goto(page, parent_url)
                        if (
                            await page.locator(implementation._COLLAPSED).count()
                            and await page.locator(".tableofcontent_action").count()
                        ):
                            await implementation._activate(
                                page, ".tableofcontent_action"
                            )
                        listing = " ".join(
                            (await page.locator("body").inner_text()).split()
                        )
                        evidence["listing_title_found"] = record["title"] in listing
                        evidence["listing_url"] = page.url
                    return evidence

                return await implementation._in_context(browser, inspect)
            finally:
                await browser.close()
    except Exception as error:
        return {"status": "failed", "errors": errors(error)}
    finally:
        _audit.reset(token)


def errors(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in errors(child)]
    return [str(error) or type(error).__name__]


def accepted(report):
    for item in report["combinations"]:
        if not item.get("equivalent") or any(
            item[form]["status"] != "success" for form in ("web", "api")
        ):
            return False
        sample = item.get("rendered_sample")
        if sample and not (
            sample.get("listing_title_found", sample.get("title_found"))
            and sample.get("category_names_found")
            and sample.get("table_route_matches")
        ):
            return False
    return all(
        item["status"] == "success"
        and not item.get("missing")
        and item.get("configured_records_equal")
        for item in report["discovery"]
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="tests/fixtures/providers.json")
    parser.add_argument("--output", default="output/compatibility.json")
    parser.add_argument("--provider", action="append")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep successful checks from the output file",
    )
    parser.add_argument(
        "--timeout", type=float, default=900, help="Maximum seconds per invocation"
    )
    args = parser.parse_args()
    install_audit()
    providers = json.loads(Path(args.providers).read_text(encoding="utf-8"))
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "implementation_sha256": hashlib.sha256(
            Path(implementation.__file__).read_bytes()
        ).hexdigest(),
        "combinations": [],
        "discovery": [],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    current_hash = report["implementation_sha256"]
    if args.resume and output.exists():
        report = json.loads(output.read_text(encoding="utf-8"))
        for section in ("combinations", "discovery"):
            for item in report[section]:
                item.setdefault(
                    "implementation_sha256", report["implementation_sha256"]
                )
        report.setdefault("resumed_runs", []).append(
            {
                "started_at": datetime.now(UTC).isoformat(),
                "implementation_sha256": current_hash,
            }
        )
        report.pop("finished_at", None)
        report.pop("accepted", None)

    def save():
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def run_provider(provider):
        for language in provider["languages"]:
            explicit = []
            for database in provider["database_ids"]:
                previous = next(
                    (
                        item
                        for item in report["combinations"]
                        if (item["provider"], item["language"], item["database_id"])
                        == (provider["code"], language, unquote(database))
                    ),
                    None,
                )
                if (
                    previous
                    and previous.get("equivalent")
                    and all(
                        previous[form]["status"] == "success" for form in ("web", "api")
                    )
                ):
                    explicit.extend(previous["records"])
                    continue
                if previous:
                    report.setdefault("previous_attempts", []).append(previous)
                    report["combinations"].remove(previous)
                result = {
                    "provider": provider["code"],
                    "language": language,
                    "database_id": unquote(database),
                    "started_at": datetime.now(UTC).isoformat(),
                    "implementation_sha256": current_hash,
                }
                outputs = {}
                for form in ("web", "api"):
                    try:
                        rows, audit = await checked_call(
                            provider[f"base_{form}_url"],
                            [language],
                            [database],
                            timeout=args.timeout,
                        )
                        outputs[form] = rows
                        result[form] = {
                            "status": "success"
                            if unquote(database) in audit["confirmed_databases"]
                            else "unsupported",
                            "count": len(rows),
                            "api_response_count": len(audit["api_responses"]),
                            "enrichment": audit["enrichment"],
                        }
                    except Exception as error:
                        audit = getattr(error, "pxweb_audit", {})
                        result[form] = {
                            "status": "failed",
                            "count": None,
                            "errors": errors(error),
                            "api_response_count": len(audit.get("api_responses", [])),
                        }
                if len(outputs) == 2:
                    result["exact_equivalent"] = outputs["web"] == outputs["api"]
                    result["equivalent"] = comparable(outputs["web"]) == comparable(
                        outputs["api"]
                    )
                    result["session_url_difference_count"] = (
                        sum(
                            a["web_url"] != b["web_url"]
                            for a, b in zip(outputs["web"], outputs["api"], strict=True)
                        )
                        if result["equivalent"]
                        else None
                    )
                    result["records"] = outputs["web"]
                    explicit.extend(outputs["web"])
                    if outputs["web"]:
                        result["rendered_sample"] = await verify_sample(
                            outputs["web"][0]
                        )
                result["finished_at"] = datetime.now(UTC).isoformat()
                report["combinations"].append(result)
                save()
                print(
                    json.dumps(
                        {k: v for k, v in result.items() if k != "records"},
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                await asyncio.sleep(provider.get("rate_limit", 1.25))
            previous = next(
                (
                    item
                    for item in report["discovery"]
                    if (item["provider"], item["language"])
                    == (provider["code"], language)
                ),
                None,
            )
            if (
                previous
                and previous["status"] == "success"
                and previous.get("configured_records_equal")
            ):
                continue
            if previous:
                report.setdefault("previous_discovery_attempts", []).append(previous)
                report["discovery"].remove(previous)
            discovery = {
                "provider": provider["code"],
                "language": language,
                "implementation_sha256": current_hash,
            }
            try:
                rows, audit = await checked_call(
                    provider["base_web_url"], [language], timeout=args.timeout
                )
                discovered = set(audit["confirmed_databases"])
                configured = {unquote(x) for x in provider["database_ids"]}

                discovery.update(
                    status="success",
                    count=len(rows),
                    missing=sorted(configured - discovered),
                    additional=sorted(discovered - configured),
                    configured_records_equal=comparable(
                        [x for x in rows if x["database_id"] in configured]
                    )
                    == comparable(explicit),
                    api_response_count=len(audit["api_responses"]),
                )
            except Exception as error:
                audit = getattr(error, "pxweb_audit", {})
                discovery.update(
                    status="failed",
                    errors=errors(error),
                    confirmed_databases=audit.get("confirmed_databases", []),
                    api_response_count=len(audit.get("api_responses", [])),
                )
            report["discovery"].append(discovery)
            save()
            print(json.dumps(discovery, ensure_ascii=True), flush=True)

    semaphore = asyncio.Semaphore(3)

    async def controlled(provider):
        async with semaphore:
            await run_provider(provider)

    await asyncio.gather(
        *(
            controlled(p)
            for p in providers
            if not args.provider or p["code"] in args.provider
        )
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["accepted"] = accepted(report)
    save()
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

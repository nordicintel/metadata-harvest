"""Complete per-language catalogue discovery, with explicit failure evidence."""

from collections.abc import AsyncIterator
from math import ceil

from harvesting.adapters.pxweb_v2.client import Client
from harvesting.adapters.pxweb_v2.validation import validate_table


def table_record(provider: dict, language: str, table: dict) -> dict:
    validate_table(table)
    return {
        "provider_code": provider["code"],
        "dataset_code": table["id"],
        "language": language,
        "base_api_url": provider["base_api_url"],
        "catalogue": {k: v for k, v in table.items() if k != "language"},
    }


async def discover_provider(
    provider: dict, *, client: Client | None = None
) -> AsyncIterator[dict]:
    """Yield one scope per configured language; only complete scopes prove absence."""
    if client is None:
        async with Client(provider["base_api_url"]) as owned:
            async for scope in discover_provider(provider, client=owned):
                yield scope
        return
    config_error = None
    try:
        config = await client.get("config")
        client.configure(config)
        supported = {row["id"] for row in config["languages"]}
    except Exception as error:
        config_error = f"{type(error).__name__}: {error}"
        supported = set()
    for language in provider["languages"]:
        scope = {
            "language": language,
            "complete": False,
            "records": [],
            "observed_ids": [],
            "errors": [],
        }
        try:
            if config_error:
                raise ValueError(config_error)
            if language not in supported:
                raise ValueError(
                    f"Configured language {language} missing from API config"
                )
            number, expected, seen = 1, None, set()
            while True:
                payload = await client.get(
                    "tables",
                    lang=language,
                    includeDiscontinued="true",
                    pageNumber=number,
                    pageSize=provider.get("page_size", 1000),
                )
                page, rows = payload.get("page"), payload.get("tables")
                if (
                    payload.get("language") != language
                    or not isinstance(page, dict)
                    or not isinstance(rows, list)
                ):
                    raise ValueError("Invalid catalogue language, page or table list")
                total, pages, size = (
                    page.get(k) for k in ("totalElements", "totalPages", "pageSize")
                )
                if (
                    any(type(v) is not int for v in (total, pages, size))
                    or total < 0
                    or size <= 0
                    or pages != max(1, ceil(total / size))
                    or page.get("pageNumber") != number
                ):
                    raise ValueError("Invalid pagination counts")
                signature = (total, pages, size)
                if expected is not None and signature != expected:
                    raise ValueError("Catalogue totals changed during pagination")
                expected = signature
                for position, table in enumerate(rows):
                    code = table.get("id") if isinstance(table, dict) else None
                    try:
                        if not isinstance(code, str) or not code.strip():
                            raise ValueError("Invalid table ID")
                        scope["observed_ids"].append(code)
                        if code in seen:
                            scope["records"] = [
                                row
                                for row in scope["records"]
                                if row["dataset_code"] != code
                            ]
                            raise ValueError("Duplicate table ID in catalogue")
                        seen.add(code)
                        if table.get("language", language) != language:
                            raise ValueError("Wrong table language")
                        scope["records"].append(table_record(provider, language, table))
                    except Exception as error:
                        scope["errors"].append(
                            f"{language} page {number} row {position} table {code!r}: "
                            f"{type(error).__name__}: {error}"
                        )
                if len(rows) != min(size, max(0, total - (number - 1) * size)):
                    raise ValueError("Missing catalogue rows")
                if number == pages:
                    break
                number += 1
            if len(seen) != total:
                raise ValueError("Catalogue count mismatch")
            scope["complete"] = not scope["errors"]
        except Exception as error:
            scope["errors"].append(f"{type(error).__name__}: {error}")
        yield scope

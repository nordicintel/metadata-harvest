"""Complete per-language catalogue discovery, with explicit failure evidence."""

import logging
from collections.abc import AsyncIterator
from math import ceil

from apps.harvest.metadata_adapters.pxweb_v2.client import Client
from apps.harvest.metadata_adapters.pxweb_v2.validation import validate_table
from registry import ServiceBinding

logger = logging.getLogger("pxweb_v2_logger")


def table_record(
    provider_code: str, base_api_url: str, language: str, table: dict
) -> dict:
    validate_table(table)
    return {
        "provider_code": provider_code,
        "dataset_code": table["id"],
        "language": language,
        "base_api_url": base_api_url,
        "catalogue": {k: v for k, v in table.items() if k != "language"},
    }


async def discover_provider(
    binding: ServiceBinding, *, client: Client | None = None
) -> AsyncIterator[dict]:
    """Yield one scope per configured language; only complete scopes prove absence."""
    if client is None:
        async with Client(binding.endpoint_url) as owned:
            async for scope in discover_provider(binding, client=owned):
                yield scope
        return
    config_error = None
    logger.info(
        "Discovering catalogue of provider %s for languages %s (page size %s)",
        binding.source_code,
        binding.options["languages"],
        binding.options.get("page_size", 1000),
    )
    try:
        config = await client.get("config")
        client.configure(config)
        supported = {row["id"] for row in config["languages"]}
        logger.info("API config loaded; supported languages: %s", sorted(supported))
    except Exception as error:
        config_error = f"{type(error).__name__}: {error}"
        supported = set()
        logger.error(
            "API config unavailable (%s); no language scope can be completed",
            config_error,
        )
    for language in binding.options["languages"]:
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
                    pageSize=binding.options.get("page_size", 1000),
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
                logger.debug(
                    "Catalogue %s page %d/%d: %d rows (total %d tables)",
                    language,
                    number,
                    pages,
                    len(rows),
                    total,
                )
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
                        scope["records"].append(
                            table_record(
                                binding.source_code,
                                binding.endpoint_url,
                                language,
                                table,
                            )
                        )
                    except Exception as error:
                        logger.debug(
                            "Rejected catalogue row %s page %d row %d table %r: %s: %s",
                            language,
                            number,
                            position,
                            code,
                            type(error).__name__,
                            error,
                        )
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
            logger.error(
                "Catalogue discovery for %s aborted: %s: %s",
                language,
                type(error).__name__,
                error,
            )
            scope["errors"].append(f"{type(error).__name__}: {error}")
        log = logger.info if scope["complete"] else logger.warning
        log(
            "Discovery scope %s: complete=%s, %d valid records, %d observed ids, "
            "%d errors%s",
            language,
            scope["complete"],
            len(scope["records"]),
            len(scope["observed_ids"]),
            len(scope["errors"]),
            ""
            if scope["complete"]
            else " (incomplete scope cannot prove any table was removed)",
        )
        yield scope

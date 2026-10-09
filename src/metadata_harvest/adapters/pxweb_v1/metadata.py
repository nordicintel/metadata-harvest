"""Build metadata-only JSON-stat2 datasets from complete PXWeb table endpoints."""

import asyncio
import logging
import math
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from metadata_harvest.adapters.pxweb_v1.discovery import _retry_after

logger = logging.getLogger(__name__)
_CELL_LIMIT = 10_000
_STANDARD = {"label", "source", "updated", "note", "link", "href", "extension", "role"}


def _merge(first, later, union=False):
    """Fill absent metadata, retaining first-source values including false/empty."""
    if first is None:
        return deepcopy(later)
    if isinstance(first, dict) and isinstance(later, dict):
        # These positions refer to the accompanying note array, not the cube.
        if isinstance(first.get("note"), list) and isinstance(later.get("note"), list):
            mandatory = (
                later.get("extension", {}).get("noteMandatory")
                if isinstance(later.get("extension"), dict)
                else None
            )
            if isinstance(mandatory, dict):
                later = deepcopy(later)
                notes = _merge(first["note"], later["note"], union=True)
                remapped = {}
                for position, value in mandatory.items():
                    if not str(position).isdigit() or int(position) >= len(
                        later["note"]
                    ):
                        raise ValueError("Invalid noteMandatory position")
                    remapped[str(notes.index(later["note"][int(position)]))] = value
                later["extension"]["noteMandatory"] = remapped
        result = deepcopy(first)
        for key, value in later.items():
            result[key] = _merge(
                result.get(key), value, union or key in {"note", "link"}
            )
        return result
    if union and isinstance(first, list) and isinstance(later, list):
        return deepcopy(first + [item for item in later if item not in first])
    return deepcopy(first)


def _input(table, provider_code):
    if (
        not isinstance(table, dict)
        or not isinstance(provider_code, str)
        or not provider_code
    ):
        raise ValueError("A table record and nonempty provider_code are required")
    for key in (
        "id",
        "title",
        "published",
        "api_url",
        "web_url",
        "language",
        "database_id",
        "path",
    ):
        if not isinstance(table.get(key), str) or not table[key]:
            raise ValueError(f"table.{key} must be a nonempty string")
    parsed = urlsplit(table["api_url"])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        raise ValueError("table.api_url must be a complete HTTP(S) endpoint")
    if not isinstance(table.get("category_path"), list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not isinstance(item.get("name"), str)
        for item in table["category_path"]
    ):
        raise ValueError("table.category_path must contain category IDs and names")
    if table.get("provider_code", provider_code) != provider_code:
        raise ValueError("provider_code disagrees with the table record")


def _config_url(url):
    parsed = urlsplit(url)
    parts = parsed.path.split("/")
    for index in range(len(parts) - 2):
        if parts[index].lower() == "api" and parts[index + 1].lower() == "v1":
            return urlunsplit(
                parsed._replace(path="/".join(parts[: index + 3]) + "/", query="config")
            )
    raise ValueError("table.api_url has no /api/v1/language/ route")


class _Requests:
    """One request context, sequential pacing, and bounded transport recovery."""

    def __init__(self, playwright):
        self.playwright = playwright
        self.context = None
        self.insecure = False
        self.interval = 1.0
        self.last = 0.0
        self.count = 0
        self.stage = "initialization"

    async def close(self):
        if self.context is not None:
            await self.context.dispose()

    async def json(self, url, payload=None, *, optional=False):
        for attempt in range(4):
            if self.context is None:
                self.context = await self.playwright.request.new_context(
                    ignore_https_errors=self.insecure
                )
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self.last + self.interval - loop.time()))
            self.last = loop.time()
            self.count += 1
            logger.info(
                "PXWeb metadata request %d: %s %s",
                self.count,
                "GET" if payload is None else "POST",
                url,
                extra={
                    "metadata_request": {
                        "method": "GET" if payload is None else "POST",
                        "url": url,
                    }
                },
            )
            response = None
            try:
                response = await self.context.fetch(
                    url,
                    method="GET" if payload is None else "POST",
                    data=payload,
                    timeout=60_000,
                )
                if optional and response.status in {400, 404, 405}:
                    logger.warning(
                        "PXWeb config unavailable at %s; using conservative limits", url
                    )
                    return {}
                if response.status == 429 or response.status >= 500:
                    if attempt < 3:
                        await asyncio.sleep(
                            _retry_after(response.headers.get("retry-after"), attempt)
                        )
                        continue
                if not response.ok:
                    raise ValueError(
                        f"HTTP {response.status}: {(await response.text())[:200]}"
                    )
                return await response.json()
            except PlaywrightError as error:
                message = str(error).lower()
                if not self.insecure and any(
                    word in message for word in ("certificate", "cert_authority", "ssl")
                ):
                    logger.warning(
                        "Certificate validation failed for %s; retrying with ignore_https_errors=True",
                        url,
                    )
                    await self.close()
                    self.context = None
                    self.insecure = True
                elif attempt == 3:
                    raise
                else:
                    await asyncio.sleep(_retry_after(None, attempt))
            finally:
                if response is not None:
                    await response.dispose()
        raise ValueError(f"Request retries exhausted: {url}")


def _variables(metadata, *, complete=False):
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("variables"), list)
        or not metadata["variables"]
    ):
        raise ValueError("GET metadata has no variables")
    seen = set()
    for variable in metadata["variables"]:
        if not isinstance(variable, dict):
            raise ValueError("Invalid variable")
        code = variable.get("code")
        if not isinstance(code, str) or not code or code in seen:
            raise ValueError("Variable codes must be unique nonempty strings")
        seen.add(code)
        values, labels = variable.get("values"), variable.get("valueTexts")
        if values is not None and (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"Invalid category codes: {code}")
        if labels is not None and (
            not isinstance(labels, list)
            or any(not isinstance(label, str) for label in labels)
            or (values is not None and len(values) != len(labels))
        ):
            raise ValueError(f"Invalid category labels: {code}")
        if complete and (values is None or labels is None):
            raise ValueError(f"Incomplete category list: {code}")
    return metadata["variables"]


def _categories(dimension, size):
    category = dimension.get("category", {})
    index, labels = category.get("index"), category.get("label")
    if index is None and size == 1 and isinstance(labels, dict) and len(labels) == 1:
        index = list(labels)
    if isinstance(index, dict):
        if any(type(i) is not int for i in index.values()) or sorted(
            index.values()
        ) != list(range(size)):
            raise ValueError("Invalid category index positions")
        index = sorted(index, key=index.__getitem__)
    if (
        not isinstance(index, list)
        or len(index) != size
        or any(not isinstance(code, str) for code in index)
        or len(set(index)) != size
    ):
        raise ValueError("Invalid category index")
    if not isinstance(labels, dict) or any(
        not isinstance(labels.get(code), str) for code in index
    ):
        raise ValueError("Missing category labels")
    return index


def _read_sample(sample, variables, selections, limit):
    """Validate selected coordinates before stripping observations."""
    if (
        not isinstance(sample, dict)
        or sample.get("class") != "dataset"
        or sample.get("version") != "2.0"
    ):
        raise ValueError("Response is not a JSON-stat2 dataset")
    sample = deepcopy(sample)
    ids, sizes, dimensions = (
        sample.get("id"),
        sample.get("size"),
        sample.get("dimension"),
    )
    expected = {variable["code"]: variable for variable in variables}
    if (
        not isinstance(ids, list)
        or any(not isinstance(code, str) for code in ids)
        or len(set(ids)) != len(ids)
        or not set(expected).issubset(ids)
    ):
        raise ValueError("POST dimension IDs differ from GET metadata")
    if (
        not isinstance(sizes, list)
        or len(sizes) != len(ids)
        or any(type(size) is not int or size < 1 for size in sizes)
        or math.prod(sizes) > limit
    ):
        raise ValueError("Invalid or oversized POST dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("Missing POST dimensions")
    coordinates = {}
    for code, size in zip(ids, sizes, strict=True):
        if not isinstance(dimensions.get(code), dict):
            raise ValueError(f"Missing POST dimension: {code}")
        codes = _categories(dimensions[code], size)
        if code not in expected:
            if (
                size != 1
                or dimensions[code].get("extension", {}).get("elimination") is not True
            ):
                raise ValueError(f"Unexpected POST dimension: {code}")
            coordinates[code] = codes
            continue
        selected = selections[code]
        # MCF uses EliminatedValue for a single selected ContentsCode even when
        # the source variable has multiple categories. Only an explicit singleton
        # selection identifies which base category the response represents.
        known = expected[code].get("values")
        replacement = selected if selected is not None else known
        if (
            codes == ["EliminatedValue"]
            and known
            and replacement
            and len(replacement) == 1
            and replacement[0] in known
            and (len(known) == 1 or code == "ContentsCode")
            and dimensions[code].get("extension", {}).get("elimination") is True
        ):

            def rekey(value, replacement=replacement[0]):
                if isinstance(value, dict):
                    return {
                        replacement if key == "EliminatedValue" else key: rekey(
                            item, replacement
                        )
                        for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [rekey(item, replacement) for item in value]
                return value

            dimensions[code] = rekey(dimensions[code])
            codes = list(replacement)
        if selected is not None and set(codes) != set(selected):
            raise ValueError(f"POST categories differ from selection: {code}")
        known = expected[code].get("values")
        if known is not None and not set(codes).issubset(known):
            raise ValueError(f"POST has unknown categories: {code}")
        coordinates[code] = codes
    clean = deepcopy(sample)
    clean.pop("value", None)
    status = clean.pop("status", None)
    if status is not None:
        extension = clean.setdefault("extension", {}).setdefault("metadata_harvest", {})
        extension["sample_status"] = [
            {"id": ids, "categories": coordinates, "status": status}
        ]
    # Some PXWeb versions attach eliminated singleton dimensions outside id/size.
    # Preserve those attributes without pretending they are selectable dimensions.
    extra = {
        code: clean["dimension"].pop(code)
        for code in list(clean["dimension"])
        if code not in expected
    }
    if extra:
        clean.setdefault("extension", {}).setdefault("metadata_harvest", {})[
            "auxiliary_dimensions"
        ] = extra
    roles = clean.get("role", {})
    for role, codes in list(roles.items()):
        if not isinstance(codes, list) or any(code not in dimensions for code in codes):
            raise ValueError("Invalid POST role references")
        auxiliary = [code for code in codes if code not in expected]
        if auxiliary:
            clean.setdefault("extension", {}).setdefault(
                "metadata_harvest", {}
            ).setdefault("auxiliary_roles", {})[role] = auxiliary
        roles[role] = [code for code in codes if code in expected]
    for code in expected:
        clean["dimension"][code]["category"].pop("index", None)
    for key in ("id", "size", "class", "version"):
        clean.pop(key, None)
    return clean, coordinates


def _payload(selections):
    return {
        "query": [
            {
                "code": code,
                "selection": {"filter": "all", "values": ["*"]}
                if values is None
                else {"filter": "item", "values": values},
            }
            for code, values in selections.items()
        ],
        "response": {"format": "json-stat2"},
    }


def _project(fields, consumed, *, dimension=False):
    standard = _STANDARD | ({"category"} if dimension else set())
    result = {
        key: deepcopy(value)
        for key, value in fields.items()
        if key in standard and key not in consumed
    }
    unknown = {
        key: deepcopy(value)
        for key, value in fields.items()
        if key not in standard and key not in consumed
    }
    if unknown:
        if result.get("extension") is None:
            result["extension"] = {}
        result.setdefault("extension", {}).setdefault("metadata_harvest", {})[
            "pxweb"
        ] = unknown
    return result


def _assemble(table, provider_code, metadata, samples):
    variables = _variables(metadata, complete=True)
    identity = deepcopy(table)
    identity.update(provider_code=provider_code, dataset_code=table["id"])
    result = {
        "class": "dataset",
        "version": "2.0",
        "label": next(
            (sample["label"] for sample in samples if sample.get("label")),
            metadata.get("title") or table["title"],
        ),
        "updated": table["published"],
        "value": [],
        "id": [variable["code"] for variable in variables],
        "size": [len(variable["values"]) for variable in variables],
        "dimension": {},
        "extension": {"metadata_harvest": identity},
    }
    result = _merge(
        result,
        {
            key: value
            for key, value in table.items()
            if key in _STANDARD and key != "updated"
        },
    )
    projected = _project(metadata, {"variables", "title"})
    if "title" in metadata:
        projected = _merge({"label": metadata["title"]}, projected)
    result = _merge(result, projected)
    for variable in variables:
        code, values = variable["code"], variable["values"]
        dimension = {
            "category": {
                "index": dict(zip(values, range(len(values)), strict=True)),
                "label": dict(zip(values, variable["valueTexts"], strict=True)),
            }
        }
        if variable.get("text") is not None:
            dimension["label"] = variable["text"]
        dimension = _merge(
            dimension,
            _project(
                variable,
                {"code", "text", "values", "valueTexts", "elimination"},
                dimension=True,
            ),
        )
        if "elimination" in variable:
            dimension.setdefault("extension", {})["elimination"] = variable[
                "elimination"
            ]
        if variable.get("time") is True:
            times = result.setdefault("role", {}).setdefault("time", [])
            if code not in times:
                times.append(code)
        result["dimension"][code] = dimension
    for sample in samples:
        # Status lists belong to different slices; retain each coordinate-scoped item.
        statuses = (
            sample.get("extension", {})
            .get("metadata_harvest", {})
            .get("sample_status", [])
        )
        existing = result["extension"]["metadata_harvest"].get("sample_status", [])
        result = _merge(result, sample)
        if statuses:
            result["extension"]["metadata_harvest"]["sample_status"] = _merge(
                existing, statuses, union=True
            )
    for code, size in zip(result["id"], result["size"], strict=True):
        _categories(result["dimension"][code], size)
    return result


async def _generate(requests, table, provider_code):
    url = table["api_url"]
    requests.stage = "provider configuration"
    config = await requests.json(_config_url(url), optional=True)
    if not isinstance(config, dict):
        raise ValueError("Provider config is not an object")
    limit = config.get("maxCells", _CELL_LIMIT)
    if type(limit) is not int or limit < 1:
        raise ValueError("Invalid provider maxCells")
    limit = min(_CELL_LIMIT, limit)
    calls, window = config.get("maxCalls"), config.get("timeWindow")
    if (
        isinstance(calls, (float, int))
        and isinstance(window, (float, int))
        and calls > 0
        and window > 0
    ):
        requests.interval = window / calls
    requests.stage = "GET table metadata"
    metadata = await requests.json(url)
    variables = _variables(metadata)
    samples = []
    covered = {variable["code"]: set() for variable in variables}

    async def fetch(selections, *, recovery=None):
        requests.stage = (
            f"recover variable {recovery}" if recovery else "POST category coverage"
        )
        payload = _payload(selections)
        if recovery:
            for entry in payload["query"]:
                if entry["code"] != recovery and selections[entry["code"]] is None:
                    entry["selection"] = {"filter": "top", "values": ["1"]}
        sample = await requests.json(url, payload)
        clean, coordinates = _read_sample(sample, variables, selections, limit)
        if recovery and any(
            len(codes) != 1 for code, codes in coordinates.items() if code != recovery
        ):
            raise ValueError("Recovery did not restrict non-target dimensions")
        samples.append(clean)
        for code, codes in coordinates.items():
            if code in covered:
                covered[code].update(codes)
        return clean, coordinates

    for variable in variables:
        if variable.get("values") is None or variable.get("valueTexts") is None:
            code = variable["code"]
            selections = {
                item["code"]: item["values"][:1] if item.get("values") else None
                for item in variables
            }
            selections[code] = variable.get("values")
            # Known lists can be chunked; unknown lists must be recovered whole.
            values, labels = [], {}
            chunks = (
                [None]
                if selections[code] is None
                else [
                    selections[code][i : i + limit]
                    for i in range(0, len(selections[code]), limit)
                ]
            )
            for chunk in chunks:
                selections[code] = chunk
                sample, coordinates = await fetch(selections, recovery=code)
                values.extend(coordinates[code])
                labels.update(sample["dimension"][code]["category"]["label"])
            if variable.get("values") is None:
                variable["values"] = values
            if variable.get("valueTexts") is None:
                variable["valueTexts"] = [labels[value] for value in variable["values"]]
    _variables(metadata, complete=True)
    selections = {variable["code"]: variable["values"][:2] for variable in variables}
    for code in reversed(selections):
        if math.prod(map(len, selections.values())) <= limit:
            break
        selections[code] = selections[code][:1]
    await fetch(selections)
    for variable in variables:
        code = variable["code"]
        pending = [value for value in variable["values"] if value not in covered[code]]
        for start in range(0, len(pending), limit):
            selections = {item["code"]: item["values"][:1] for item in variables}
            selections[code] = pending[start : start + limit]
            await fetch(selections)
    if any(covered[item["code"]] != set(item["values"]) for item in variables):
        raise ValueError("Incomplete category coverage")
    requests.stage = "assemble metadata"
    result = _assemble(table, provider_code, metadata, samples)
    logger.info(
        "PXWeb metadata complete: %s; requests=%d categories=%d",
        url,
        requests.count,
        sum(map(len, covered.values())),
    )
    from metadata_harvest.mapping import map_metadata

    record = {
        **deepcopy(table),
        "provider_code": provider_code,
        "dataset_code": table["id"],
    }
    return map_metadata(
        result, record, "pxweb_v1", max_cells=config.get("maxCells", _CELL_LIMIT)
    )


async def get_pxweb_metadata(table: dict, *, provider_code: str) -> dict:
    """Fetch GET and category-covering POST metadata as one metadata-only dataset.

    Args:
        table: A record returned by get_pxweb_tables, including its full API URL.
        provider_code: Caller-supplied provider identifier.

    Returns:
        JSON-stat2 dataset with complete categories and an empty observation array.

    Raises:
        ValueError: Invalid input, malformed metadata or incomplete category coverage.
        RuntimeError: Retrieval or assembly failed; includes the table endpoint.
    """
    _input(table, provider_code)
    _config_url(table["api_url"])
    async with async_playwright() as playwright:
        requests = _Requests(playwright)
        try:
            return await _generate(requests, deepcopy(table), provider_code)
        except Exception as error:
            raise RuntimeError(
                f"PXWeb metadata generation failed during {requests.stage} for {table['api_url']}: {error}"
            ) from error
        finally:
            await requests.close()

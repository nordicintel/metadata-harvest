"""Coordinate-safe live retrieval helpers; deliberately no harvest imports."""

import math
from collections.abc import Mapping, Sequence
from itertools import product

import aiohttp

from retrieval._semantics import category_codes, validate_cube


def split_selection(selection: Mapping[str, Sequence[str]], limit: int):
    """Partition a rectangle into disjoint bounded rectangles."""
    pending = [dict(selection)]
    while pending:
        part = pending.pop()
        if math.prod(map(len, part.values())) <= limit:
            yield part
            continue
        key = max(part, key=lambda k: len(part[k]))
        middle = len(part[key]) // 2
        if middle == 0:
            raise ValueError("Provider limit cannot fit one cell")
        pending.extend(
            [{**part, key: part[key][middle:]}, {**part, key: part[key][:middle]}]
        )


def _fragment(response, selection, dimensions, auxiliary=None):
    auxiliary = auxiliary or {}
    ids = response["id"]
    axes = {key: category_codes(response["dimension"][key]["category"]) for key in ids}
    fragment = {
        "id": list(ids),
        "size": response["size"],
        "dimension": {key: {"category": {"index": axes[key]}} for key in ids},
        "value": response["value"],
    }
    if "status" in response:
        fragment["status"] = response["status"]
    validate_cube(fragment)
    if fragment["value"] == []:
        raise ValueError("Provider returned metadata instead of observations")
    for key in ids:
        if key in selection:
            if (
                axes[key] == ["EliminatedValue"]
                and len(selection[key]) == 1
                and response["dimension"][key].get("extension", {}).get("elimination")
                is True
                and (
                    category_codes(dimensions.get(key, {}).get("category", {}))
                    == list(selection[key])
                    or (
                        key == "ContentsCode"
                        and selection[key][0]
                        in category_codes(dimensions[key]["category"])
                    )
                )
            ):
                axes[key] = list(selection[key])
            if set(axes[key]) != set(selection[key]):
                raise ValueError(f"Provider returned different categories for {key}")
        elif len(axes[key]) != 1 or not (
            (
                key in auxiliary
                and response["dimension"][key].get("extension", {}).get("elimination")
                is True
                and auxiliary[key].get("extension", {}).get("elimination") is True
                and axes[key] == category_codes(auxiliary[key]["category"])
            )
            or dimensions.get(key, {})
            .get("extension", {})
            .get("nordicintel", {})
            .get("elimination")
            is True
        ):
            raise ValueError(f"Unexpected result dimension: {key}")
    if set(selection) - set(ids):
        raise ValueError("Provider omitted a selected dimension")
    return fragment, axes


def _indexed(values, total, *, scalar=False):
    if scalar and isinstance(values, str):
        return {i: values for i in range(total)}
    if isinstance(values, list):
        return dict(enumerate(values))
    if isinstance(values, dict):
        return {int(k): v for k, v in values.items()}
    raise ValueError("Invalid observations or status")


async def retrieve_selection(
    session: aiohttp.ClientSession,
    *,
    selection: Mapping[str, Sequence[str]],
    data_url: str | None,
    metadata_url: str | None,
    metadata: Mapping,
    adapter: str,
    request,
    parse=None,
) -> dict:
    """Execute adapter-built requests and merge returned cells by their codes."""
    if not data_url:
        raise ValueError("Harvested data_url is required")
    if not selection or any(
        not isinstance(k, str)
        or not v
        or isinstance(v, str)
        or len(set(v)) != len(v)
        or any(not isinstance(code, str) for code in v)
        for k, v in selection.items()
    ):
        raise ValueError("Selection requires unique nonempty category lists")
    dimensions = metadata.get("/dimension", {})
    for key, dimension in dimensions.items():
        if key not in selection:
            shared = dimension.get("extension", {}).get("nordicintel", {})
            if (
                shared.get("elimination") is not True
                or shared.get("elimination_value") is not None
            ):
                raise ValueError(f"Dimension cannot be omitted: {key}")
    limit = metadata.get(f"/extension/{adapter}", {}).get("max_cells", 10000)
    if type(limit) is not int or limit <= 0:
        raise ValueError("Invalid harvested cell limit")
    ids = list(selection)
    codes = {key: list(selection[key]) for key in ids}
    positions = {key: {value: i for i, value in enumerate(codes[key])} for key in ids}
    values, statuses = {}, {}
    pending = list(reversed(list(split_selection(codes, limit))))
    while pending:
        part = pending.pop()
        async with session.post(data_url, **request(part)) as response:
            if response.status in {404, 410}:
                if not metadata_url:
                    response.raise_for_status()
                async with session.get(metadata_url) as confirmation:
                    if confirmation.status in {404, 410}:
                        raise LookupError("Dataset no longer exists at the provider")
                    confirmation.raise_for_status()
                raise ValueError("Provider rejected the selected data request")
            if response.status in {400, 413, 422}:
                cells = math.prod(map(len, part.values()))
                if cells > 1:
                    pending.extend(
                        reversed(list(split_selection(part, max(1, cells // 2))))
                    )
                    continue
                raise ValueError(f"Provider rejected selection: HTTP {response.status}")
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if parse is not None:
            payload = parse(payload)
        source = metadata.get(f"/extension/{adapter}", {})
        auxiliary = source.get("harvest_metadata", {}).get(
            "auxiliary_dimensions",
            source.get("source_record", {}).get("auxiliary_dimensions", {}),
        )
        fragment, axes = _fragment(payload, part, dimensions, auxiliary)
        total = math.prod(fragment["size"])
        source_values = _indexed(fragment["value"], total)
        source_flags = _indexed(fragment.get("status", {}), total, scalar=True)
        coordinates = list(product(*(axes[key] for key in fragment["id"])))
        for index in source_values.keys() | source_flags.keys():
            point = dict(zip(fragment["id"], coordinates[index], strict=True))
            target = 0
            for key in ids:
                target = target * len(codes[key]) + positions[key][point[key]]
            if index in source_values:
                value = source_values[index]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (str, int, float, type(None)))
                    or isinstance(value, float)
                    and not math.isfinite(value)
                ):
                    raise ValueError("Invalid provider observation")
                if target in values:
                    raise ValueError("Overlapping provider response cells")
                values[target] = value
            if index in source_flags and source_flags[index] is not None:
                if not isinstance(source_flags[index], str):
                    raise ValueError("Invalid provider status")
                statuses[target] = source_flags[index]
    total = math.prod(map(len, codes.values()))
    result = {
        "id": ids,
        "size": [len(codes[k]) for k in ids],
        "dimension": {key: {"category": {"index": codes[key]}} for key in ids},
        "value": [values[i] for i in range(total)]
        if len(values) == total
        else {str(i): values[i] for i in sorted(values)},
    }
    if statuses:
        result["status"] = {str(i): statuses[i] for i in sorted(statuses)}
    validate_cube(result, selection=selection)
    return result

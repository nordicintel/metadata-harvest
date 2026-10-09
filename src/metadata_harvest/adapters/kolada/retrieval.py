"""On-demand Kolada observations; caller owns HTTP policy and connections."""

from urllib.parse import quote

from metadata_harvest.semantics import category_codes, validate_cube

REQUIRED_METADATA = ("/dimension", "/extension/kolada")


async def retrieve(
    session,
    *,
    provider_code,
    dataset_code,
    language,
    selection,
    data_url,
    metadata_url,
    metadata,
) -> dict:
    """Retrieve selected coordinates without caching or persisting observations."""
    native = metadata.get("/extension/kolada", {})
    scope, identifier = native.get("scope"), native.get("kpi_id")
    if (
        provider_code != "kolada"
        or language != "sv"
        or scope not in {"municipality", "ou"}
        or dataset_code != f"{scope}/{identifier}"
        or not data_url
    ):
        raise ValueError("Invalid Kolada identity or missing harvested routing")
    dimensions = metadata.get("/dimension", {})
    required = {"year", scope} | ({"gender"} if "gender" in dimensions else set())
    if set(selection) != required:
        raise ValueError("Select every published Kolada dimension")
    for key, codes in selection.items():
        if (
            isinstance(codes, str)
            or not codes
            or len(codes) != len(set(codes))
            or not set(codes) <= set(category_codes(dimensions[key]["category"]))
        ):
            raise ValueError(f"Invalid selection: {key}")
    positions = {k: {code: i for i, code in enumerate(v)} for k, v in selection.items()}
    result = {
        "id": list(selection),
        "size": list(map(len, selection.values())),
        "dimension": {
            k: {"category": {"index": dict(pos)}} for k, pos in positions.items()
        },
        "value": {},
    }
    flags, seen = {}, {}
    genders = selection.get("gender", ["t"])
    for start in range(0, len(selection[scope]), 25):
        areas = selection[scope][start : start + 25]
        for offset in range(0, len(selection["year"]), 25):
            years = selection["year"][offset : offset + 25]
            url = (
                data_url.rstrip("/")
                + f"/{scope}/"
                + ",".join(quote(x, safe="") for x in areas)
                + "/year/"
                + ",".join(quote(x, safe="") for x in years)
            )
            previous = None
            for page in range(1, 5001):
                async with session.get(
                    url, params={"page": page, "per_page": 5000}
                ) as response:
                    if response.status in {404, 410}:
                        raise LookupError(dataset_code)
                    if response.status == 400:
                        raise ValueError("Kolada rejected the selection")
                    response.raise_for_status()
                    payload = await response.json()
                rows = payload.get("values")
                if (
                    not isinstance(rows, list)
                    or (not rows and payload.get("next_url"))
                    or (rows and rows == previous)
                ):
                    raise ValueError("Malformed or repeated Kolada page")
                for row in rows:
                    year, area = str(row.get("period")), row.get(scope)
                    if (
                        row.get("kpi") != identifier
                        or year not in years
                        or area not in areas
                    ):
                        raise ValueError("Kolada returned unrequested coordinates")
                    for value in row["values"]:
                        gender = value["gender"].lower()
                        if gender not in genders or value.get("isdeleted"):
                            continue
                        coords = {"year": year, scope: area, "gender": gender}
                        index = 0
                        for key in selection:
                            index = (
                                index * len(selection[key])
                                + positions[key][coords[key]]
                            )
                        key = str(index)
                        raw, status = value.get("value"), value.get("status")
                        flag = {"Missing": "m", "Privacy": "p"}.get(status, status)
                        if raw is None and not flag:
                            flag = "e"
                        if key in seen and seen[key] != (raw, flag):
                            raise ValueError("Conflicting Kolada coordinates")
                        seen[key] = (raw, flag)
                        if raw is not None:
                            result["value"][key] = raw
                        if flag:
                            flags[key] = flag
                if not payload.get("next_url"):
                    break
                previous = rows
            else:
                raise ValueError("Kolada pagination exceeded 5000 pages")
    if flags:
        result["status"] = flags
    validate_cube(result)
    return result

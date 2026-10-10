"""Pure coverage bookkeeping over flattened Kolada cells; no I/O."""

GROUP_PREFIX = "G"


def empty(scope: str) -> dict:
    result = {"years": [], "genders": []}
    if scope == "ou":
        result["ou_ids"] = []
    else:
        result.update(municipality_ids=[], municipality_group_ids=[])
    return result


def merge(scope: str, coverage: dict | None, records, *, entities=None) -> dict:
    """Add the cells' years, entities and genders to an existing coverage.

    Deleted cells are ignored here; the caller rescans such KPIs from scratch.
    ``entities`` restricts municipality-scope cells to the configured inventory.
    """
    result = {k: set(v) for k, v in (coverage or empty(scope)).items()}
    result.setdefault("years", set())
    result.setdefault("genders", set())
    for record in records:
        if record["deleted"]:
            continue
        if scope == "municipality":
            if entities is not None and record["entity"] not in entities:
                continue
            key = (
                "municipality_group_ids"
                if record["entity"].startswith(GROUP_PREFIX)
                else "municipality_ids"
            )
        else:
            key = "ou_ids"
        result.setdefault(key, set()).add(record["entity"])
        result["years"].add(record["period"])
        result["genders"].add(record["gender"])
    return {k: sorted(v) for k, v in result.items()}


def rebuild(scope: str, records, *, entities=None) -> dict:
    """Coverage of a complete rescan: only what the source currently serves."""
    return merge(scope, None, records, entities=entities)

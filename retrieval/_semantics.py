"""Dependency-free JSON-stat relationships shared by metadata and retrieval."""

from collections.abc import Mapping
from math import prod


def category_codes(category: Mapping) -> list[str]:
    """Read category order without relying on JSON object insertion order."""
    index = category.get("index")
    if isinstance(index, list):
        codes = index
    elif isinstance(index, dict):
        if any(type(v) is not int for v in index.values()) or sorted(
            index.values()
        ) != list(range(len(index))):
            raise ValueError("Category positions must be unique and contiguous")
        codes = sorted(index, key=index.__getitem__)
    else:
        codes = list(category.get("label", {}))
        if len(codes) != 1:
            raise ValueError(
                "Category index is required except for a single labelled category"
            )
    if (
        not codes
        or any(not isinstance(v, str) or not v.strip() for v in codes)
        or len(set(codes)) != len(codes)
    ):
        raise ValueError("Invalid or duplicate category codes")
    return codes


def _validate_hierarchy(children, known, code):
    visiting, done = set(), set()

    def visit(parent):
        if parent not in known or parent in visiting:
            raise ValueError(f"Invalid category hierarchy: {code}")
        if parent in done:
            return
        visiting.add(parent)
        for child in children.get(parent, []):
            visit(child)
        visiting.remove(parent)
        done.add(parent)

    for parent in children:
        visit(parent)


def validate_cube(document: Mapping, *, selection: Mapping | None = None) -> None:
    """Check dimensions, category references, roles and observation coordinates."""
    ids, sizes, dimensions = document["id"], document["size"], document["dimension"]
    if (
        len(ids) != len(set(ids))
        or set(ids) != set(dimensions)
        or len(ids) != len(sizes)
    ):
        raise ValueError("Dimension membership/order mismatch")
    if selection is not None and set(selection) != set(ids):
        raise ValueError("Result dimensions do not match selection")
    for code, size in zip(ids, sizes, strict=True):
        dimension = dimensions[code]
        category = dimension["category"]
        codes = category_codes(category)
        known = set(codes)
        if type(size) is not int or size != len(codes):
            raise ValueError(f"Category count mismatch: {code}")
        if selection is not None and (
            known != set(selection[code]) or len(selection[code]) != len(known)
        ):
            raise ValueError(f"Result categories do not match selection: {code}")
        for field in ("label", "note", "unit", "coordinates"):
            if set(category.get(field, {})) - known:
                raise ValueError(f"Unknown category in {code}/{field}")
        children = category.get("child", {})
        _validate_hierarchy(children, known, code)
        shared = dimension.get("extension", {}).get("nordicintel", {})
        if (
            shared.get("elimination_value") is not None
            and shared["elimination_value"] not in known
        ):
            raise ValueError(f"Invalid elimination value: {code}")
        for namespace in dimension.get("extension", {}).values():
            if set(namespace.get("categories", {})) - known:
                raise ValueError(f"Unknown namespaced category: {code}")
        for links in dimension.get("link", {}).values():
            for link in links:
                reference = (
                    link.get("extension", {}).get("nordicintel", {}).get("category_id")
                )
                if reference is not None and reference not in known:
                    raise ValueError(f"Unknown resource category: {code}")
    roles = [v for members in document.get("role", {}).values() for v in members]
    if set(roles) - set(ids) or len(roles) != len(set(roles)):
        raise ValueError("Invalid or overlapping dimension roles")
    total = prod(sizes)
    for field in ("value", "status"):
        values = document.get(field)
        if isinstance(values, list) and not (field == "value" and values == []):
            if len(values) != total:
                raise ValueError(f"Dense {field} length does not match cube")
        elif isinstance(values, dict):
            if any(
                not isinstance(k, str)
                or not k.isascii()
                or not k.isdecimal()
                or str(int(k)) != k
                or int(k) >= total
                for k in values
            ):
                raise ValueError(f"Sparse {field} index out of bounds")

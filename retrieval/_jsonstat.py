"""JSON-stat 1 fallback for PXWeb v1 APIs that lack JSON-stat2 output."""

from urllib.parse import urlsplit

# Hosts whose PXWeb v1 API answers JSON-stat2 POSTs with HTTP 404 but serves
# JSON-stat. Hosts found at runtime are added for the rest of the process.
JSONSTAT1_HOSTS = {"pxweb.umea.se", "statistik.vasteras.se"}


def uses_jsonstat1(url: str) -> bool:
    return urlsplit(url).hostname in JSONSTAT1_HOSTS


def mark_jsonstat1(url: str) -> None:
    JSONSTAT1_HOSTS.add(urlsplit(url).hostname)


def jsonstat2(document: dict) -> dict:
    """Convert a single-dataset JSON-stat 1 response into a JSON-stat2 dataset."""
    if not isinstance(document, dict) or len(document) != 1:
        raise ValueError("Response is not a single-dataset JSON-stat response")
    dataset = dict(next(iter(document.values())))
    dimension = dict(dataset.get("dimension") or {})
    ids, sizes = dimension.pop("id", None), dimension.pop("size", None)
    if not isinstance(ids, list) or not isinstance(sizes, list):
        raise ValueError("JSON-stat response lacks dimension id and size")
    role = dimension.pop("role", None)
    result = {"class": "dataset", "version": "2.0"}
    result.update(
        (key, value)
        for key, value in dataset.items()
        if key not in {"class", "version", "dimension"}
    )
    result.update(id=ids, size=sizes, dimension=dimension)
    if role:
        result["role"] = role
    return result

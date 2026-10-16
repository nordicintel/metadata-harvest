"""Dispatch observation retrieval from a worker document, without repository models.

A worker document is one JSON-stat2 metadata document with an
``extension.nordicintel`` block naming the adapter, the provider, the dataset code,
the language and the provider's data and metadata URLs. Only those operational
fields and the dimension categories are read; descriptive fields are ignored and
malformed operational fields fail before any I/O.
"""

from collections.abc import Mapping, Sequence
from importlib import import_module
from urllib.parse import urlsplit

from retrieval._semantics import category_codes, validate_cube

ADAPTERS = frozenset(
    {
        "pxweb_v1",
        "pxweb_v2",
        "kolada",
        "uka",
        "sdb",
        "trafa",
        "entryscape",
        "smhi_emissions",
        "forsakringskassan",
        "riksbank",
        "dataanalys",
        "tillvaxtverket",
    }
)


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid {field}")
    return value


def _mapping(value, field):
    if not isinstance(value, Mapping):
        raise ValueError(f"Missing or invalid {field}")
    return value


def _url(value, field):
    _text(value, field)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid {field}")
    return value


async def retrieve_distribution(
    session, document: Mapping, *, selection: Mapping[str, Sequence[str]]
) -> dict:
    """Retrieve observations for one worker document using the caller's session."""
    document = _mapping(document, "document")
    shared = _mapping(
        _mapping(document.get("extension"), "extension").get("nordicintel"),
        "extension.nordicintel",
    )
    adapter = _text(shared.get("adapter"), "adapter")
    if adapter not in ADAPTERS:
        raise ValueError(f"Unknown retrieval adapter: {adapter}")
    provider = _text(shared.get("provider_code"), "provider code")
    dataset = _text(shared.get("dataset_code"), "dataset code")
    language = _text(shared.get("language"), "language")
    data_url = _url(shared.get("data_url"), "data URL")
    metadata_url = shared.get("metadata_url")
    if metadata_url is not None:
        _url(metadata_url, "metadata URL")
    try:
        validate_cube(document)
        available = {
            code: set(category_codes(_mapping(axis, "dimension")["category"]))
            for code, axis in document["dimension"].items()
        }
    except (KeyError, TypeError, IndexError, AttributeError, ValueError) as error:
        raise ValueError(f"Malformed retrieval structure: {error}") from error
    selection = _mapping(selection, "selection")
    if not selection:
        raise ValueError("Selection must not be empty")
    for code, values in selection.items():
        if (
            code not in available
            or isinstance(values, str | bytes)
            or not isinstance(values, Sequence)
            or not values
        ):
            raise ValueError(f"Invalid selection: {code}")
        if (
            any(not isinstance(v, str) for v in values)
            or len(set(values)) != len(values)
            or not set(values) <= available[code]
        ):
            raise ValueError(f"Invalid selection: {code}")
    module = import_module(f"retrieval.data_adapters.{adapter}")
    resolved = {}
    for pointer in module.REQUIRED_METADATA:
        value = document
        try:
            for part in pointer.strip("/").split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as error:
            raise ValueError(f"Missing retrieval metadata: {pointer}") from error
        resolved[pointer] = value
    return await module.retrieve(
        session,
        provider_code=provider,
        dataset_code=dataset,
        language=language,
        selection=selection,
        data_url=data_url,
        metadata_url=metadata_url,
        metadata=resolved,
    )

"""Read operational fields from compact catalog JSON-LD, without repository models."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from importlib import import_module
from urllib.parse import urlsplit

from retrieval._semantics import validate_cube

ADAPTERS = frozenset(
    {
        "pxweb_v1",
        "pxweb_v2",
        "kolada",
        "uka",
        "sdb",
        "trafa",
        "entrysoft",
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


def _codes(value, field):
    if not isinstance(value, list) or not value:
        raise ValueError(f"Missing or invalid {field}")
    for code in value:
        _text(code, field)
    if len(set(value)) != len(value):
        raise ValueError(f"Duplicate {field}")
    return value


def _url(value, field):
    _text(value, field)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid {field}")
    return value


def _operational_metadata(distribution, binding, adapter):
    """Project only request metadata used by the existing adapter implementations."""
    structure = _mapping(distribution.get("ni:structure"), "ni:structure")
    components = structure.get("qb:component")
    axes = binding.get("dimensions")
    if not isinstance(axes, list) or not axes or not isinstance(components, list):
        raise ValueError("Missing retrieval dimensions or structure")
    if len(axes) != len(components):
        raise ValueError("Retrieval and structure dimensions differ")
    dimensions, roles, ids = {}, {}, []
    for position, (axis, component) in enumerate(zip(axes, components, strict=True), 1):
        axis = _mapping(axis, "query dimension")
        component = _mapping(component, "structure component")
        dimension = _mapping(component.get("qb:dimension"), "dimension")
        code = _text(axis.get("code"), "dimension code")
        if code in ids or dimension.get("dct:identifier") != code:
            raise ValueError("Duplicate or inconsistent dimension identity")
        if (
            type(component.get("qb:order")) is not int
            or component["qb:order"] != position
        ):
            raise ValueError("Invalid structure order")
        ids.append(code)
        selectable = _codes(axis.get("categories"), "selectable categories")
        scheme = _mapping(dimension.get("qb:codeList"), "dimension codelist")
        concepts = scheme.get("codes")
        if not isinstance(concepts, list):
            raise ValueError("Missing codelist codes")
        known = [
            _text(_mapping(c, "concept").get("skos:notation"), "category code")
            for c in concepts
        ]
        if len(known) != len(set(known)) or set(known) != set(selectable):
            raise ValueError("Retrieval and structure categories differ")
        encoding = axis.get("index_encoding", "list")
        if encoding not in {"list", "dict", "omitted"}:
            raise ValueError("Invalid category index encoding")
        category = {}
        if encoding != "omitted":
            category["index"] = (
                dict(zip(selectable, range(len(selectable)), strict=True))
                if encoding == "dict"
                else list(selectable)
            )
        language = binding["source"]["language"]
        for concept in concepts:
            native_code = concept["skos:notation"]
            for native, field in (
                ("label", "skos:prefLabel"),
                ("note", "skos:note"),
                ("unit", "ni:unit"),
                ("coordinates", "ni:coordinates"),
            ):
                value = concept.get(field)
                if native in {"label", "note"}:
                    value = (value or {}).get(language)
                    if value and native == "label":
                        value = value[0]
                if value is not None and value != {} and value != []:
                    category.setdefault(native, {})[native_code] = deepcopy(value)
        parents = {c.get("@id"): c["skos:notation"] for c in concepts if c.get("@id")}
        for concept in concepts:
            for parent in concept.get("skos:broader", []):
                if parent not in parents:
                    raise ValueError("Unknown category parent")
                category.setdefault("child", {}).setdefault(parents[parent], []).append(
                    concept["skos:notation"]
                )
        for values in scheme.get("ni:additionalAttributes", {}).values():
            category.update(deepcopy(values))
        extensions = deepcopy(dimension.get("ni:additionalAttributes", {}))
        item = {"category": category}
        item.update(extensions.pop(adapter + ":fields", {}))
        for native, field in (
            ("elimination", "ni:elimination"),
            ("elimination_value", "ni:eliminationValue"),
        ):
            value = dimension.get(field)
            if value is not None:
                if native == "elimination" and type(value) is not bool:
                    raise ValueError("Invalid elimination setting")
                if native == "elimination_value" and value not in selectable:
                    raise ValueError("Unknown elimination category")
                extensions.setdefault("nordicintel", {})[native] = value
        if extensions:
            item["extension"] = extensions
        if dimension.get("skos:prefLabel", {}).get(language):
            item["label"] = dimension["skos:prefLabel"][language][0]
        if dimension.get("skos:note", {}).get(language):
            item["note"] = deepcopy(dimension["skos:note"][language])
        if dimension.get("ni:links"):
            item["link"] = deepcopy(dimension["ni:links"])
        dimensions[code] = item
        role = dimension.get("ni:role")
        if role is not None:
            if role not in {"time", "geo", "metric"}:
                raise ValueError("Invalid dimension role")
            roles.setdefault(role, []).append(code)
    options = deepcopy(_mapping(binding.get("options", {}), "retrieval options"))
    extras = options.pop("ni:additionalAttributes", {})
    options.update(extras.get("custom", {}))
    evidence = distribution.get("ni:sourceEvidence", {})
    extensions = deepcopy(evidence.get("extensions", {}))
    extensions[adapter] = options
    metadata = {
        "id": ids,
        "dimension": dimensions,
        "role": roles,
        "extension": extensions,
    }
    # Old projection preserved explicitly recorded empty operational fields.
    for path, value in evidence.get("empty_values", {}).items():
        parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")]
        if parts[0] not in metadata:
            continue
        target = metadata
        for part in parts[:-1]:
            target = (
                target[int(part)]
                if isinstance(target, list)
                else target.setdefault(part, {})
            )
        if isinstance(target, list):
            target[int(parts[-1])] = deepcopy(value)
        else:
            target[parts[-1]] = deepcopy(value)
    validate_cube(
        {**metadata, "size": [len(a["categories"]) for a in axes], "value": []}
    )
    return metadata


async def retrieve_distribution(
    session, distribution: Mapping, *, selection: Mapping[str, Sequence[str]]
) -> dict:
    """Retrieve a compact catalog distribution using the caller's session.

    No context fetching, configuration lookup, or metadata profile migration occurs.
    Descriptive fields are ignored; malformed operational fields fail before I/O.
    """
    distribution = _mapping(distribution, "distribution")
    binding = _mapping(distribution.get("ni:retrieval"), "ni:retrieval")
    source = _mapping(binding.get("source"), "retrieval source")
    adapter = _text(source.get("adapter"), "adapter")
    if adapter not in ADAPTERS:
        raise ValueError(f"Unknown retrieval adapter: {adapter}")
    provider = _text(
        source.get("source_code", source.get("provider_code")), "source code"
    )
    dataset = _text(source.get("dataset_code"), "dataset code")
    language = _text(source.get("language"), "language")
    urls = distribution.get("dcat:accessURL")
    if not isinstance(urls, list) or not urls:
        raise ValueError("Missing dcat:accessURL")
    data_url = _url(urls[0], "access URL")
    metadata_url = binding.get("metadata_url")
    if metadata_url is not None:
        _url(metadata_url, "metadata URL")
    try:
        metadata = _operational_metadata(distribution, binding, adapter)
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError(f"Malformed retrieval structure: {error}") from error
    selection = _mapping(selection, "selection")
    if not selection:
        raise ValueError("Selection must not be empty")
    available = {
        axis["code"]: set(axis["categories"]) for axis in binding["dimensions"]
    }
    for code, values in selection.items():
        if (
            code not in available
            or isinstance(values, (str, bytes))
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
        value = metadata
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

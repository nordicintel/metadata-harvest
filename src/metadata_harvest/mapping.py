"""PXWeb source-to-contract mapping with explicit retention of source extras."""

import re
from copy import deepcopy
from datetime import date
from urllib.parse import quote, urlsplit, urlunsplit

from metadata_harvest.validation import validate_metadata, validator


def source_record(document: dict, adapter: str | None = None) -> dict:
    """Read accepted listing evidence, including pre-consolidation documents."""
    extension = document.get("extension", {})
    if adapter and "source_record" in extension.get(adapter, {}):
        return extension[adapter]["source_record"]
    for name in ("pxweb_v1", "pxweb_v2"):
        if "source_record" in extension.get(name, {}):
            return extension[name]["source_record"]
    return extension["metadata_harvest"]


def source_date(value):
    """Extract the source calendar date without converting its timezone."""
    match = re.match(r"^((?:19|20)\d{2}-\d{2}-\d{2})", str(value))
    if match:
        try:
            return date.fromisoformat(match[1]).isoformat()
        except ValueError:
            pass
    return None


def _contacts(values):
    result = []
    for value in values:
        if not isinstance(value, dict):
            value = {"raw": value}
        contact = deepcopy(value)
        if "mail" in contact:
            contact["email"] = contact.pop("mail")
        if contact.get("raw"):
            raw = str(contact["raw"])
            match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)
            if match and not contact.get("email"):
                contact["email"] = match[0]
            # Keep unparsed prose, but not an exact concatenation of parsed fields.
            remainder = raw
            for key in ("name", "email", "phone", "organization"):
                if contact.get(key):
                    remainder = remainder.replace(str(contact[key]), "")
            if not remainder.strip(" #+,;\n\r"):
                contact.pop("raw")
        if contact:
            result.append(contact)
    return result


def _links(item, adapter):
    for links in item.get("link", {}).values():
        for link in links:
            if link.get("extension"):
                link["extension"] = {adapter: link["extension"]}


def _retain_optional_source_fields(document, adapter):
    """Relocate nonconforming optional source fields, never cube structure.

    Native PXWeb permits fields/values outside the JSON-stat profile. Their exact
    values are retained under JSON Pointer keys rather than guessed or discarded.
    Identity, labels and cube/category identities cannot be repaired this way.
    """
    retained = document["extension"][adapter].setdefault("source_fields", {})
    for _ in range(1000):
        errors = list(validator("dataset-metadata").iter_errors(document))
        if not errors:
            if not retained:
                document["extension"][adapter].pop("source_fields")
            return
        error = errors[0]
        path = list(error.absolute_path)
        if error.validator == "additionalProperties" and isinstance(
            error.instance, dict
        ):
            allowed = error.schema.get("properties", {})
            patterns = error.schema.get("patternProperties", {})
            extras = [
                k
                for k in error.instance
                if k not in allowed and not any(re.search(p, k) for p in patterns)
            ]
            if extras:
                path.append(extras[0])
        # A malformed link must remain source evidence, not a fabricated resource.
        if "link" in path:
            position = path.index("link")
            if len(path) > position + 2 and isinstance(path[position + 2], int):
                path = path[: position + 3]
        if (
            not path
            or path[0] in {"id", "size", "value", "class", "version", "label"}
            or "index" in path
        ):
            raise ValueError(
                f"Invalid required source structure: {error.json_path}: {error.message}"
            )
        if path[0] == "dimension" and (
            len(path) < 3 or path[-1] in {"category", "label"} and len(path) == 3
        ):
            raise ValueError(f"Invalid required dimension: {error.message}")
        if path[:2] == ["extension", "nordicintel"]:
            raise ValueError(
                f"Invalid shared mapping: {error.json_path}: {error.message}"
            )
        parent = document
        for component in path[:-1]:
            parent = parent[component]
        key = path[-1]
        pointer = "/" + "/".join(
            str(p).replace("~", "~0").replace("/", "~1") for p in path
        )
        if isinstance(parent, list):
            # Preserve the entire original array so pointer indices stay meaningful.
            container = document
            for component in path[:-2]:
                container = container[component]
            array_pointer = pointer.rsplit("/", 1)[0]
            retained.setdefault(array_pointer, deepcopy(parent))
            parent.pop(key)
        else:
            retained[pointer] = parent.pop(key)
    raise ValueError("Too many nonconforming optional source fields")


def _url(value):
    url = urlsplit(value)
    return urlunsplit(
        (
            url.scheme,
            url.netloc,
            quote(url.path, safe="/%:@!$&'()*+,;="),
            quote(url.query, safe="%:@!$&'()*+,;=/?"),
            quote(url.fragment, safe="%/?"),
        )
    )


def v1_listing_fields(record):
    """Canonical fields derived from current listing routes and thematic paths."""
    shared = {}
    shared.update(metadata_url=record["api_url"], data_url=record["api_url"])
    if record.get("web_url"):
        shared["source_url"] = record["web_url"]
    paths = []
    for location in record.get("paths", [record]):
        path = [
            {
                "id": node["id"],
                "label": node.get("name", node.get("label", node["id"])),
            }
            for node in location.get("category_path", [])
        ]
        if path and path not in paths:
            paths.append(path)
    if paths:
        shared["paths"] = paths
    for key in ("data_url", "metadata_url", "source_url"):
        if key in shared:
            shared[key] = _url(shared[key])
    return shared


def map_metadata(
    native: dict, record: dict, adapter: str, *, max_cells: int | None = None
) -> dict:
    """Map complete native metadata to the shared profile without source loss."""
    if adapter not in {"pxweb_v1", "pxweb_v2"}:
        raise ValueError(f"Unsupported mapping: {adapter}")
    if "nordicintel" in native.get("extension", {}):
        result = deepcopy(native)
        validate_metadata(result, adapter=adapter)
        return result
    result = deepcopy(native)
    extra = result.pop("extension", {})
    saved = extra.pop("metadata_harvest", {})
    shared = {key: record[key] for key in ("provider_code", "dataset_code", "language")}
    catalogue = record.get("catalogue", {})
    original_date = result.pop("updated", None)
    if parsed := source_date(original_date):
        result["updated"] = parsed
    source = {**extra, "source_record": deepcopy(record)}
    saved_extras = {k: v for k, v in saved.items() if k not in record}
    if saved_extras:
        source["harvest_metadata"] = saved_extras
    if original_date is not None:
        source["updated_original"] = original_date
    contacts = source.pop("contact", [])
    if contacts:
        shared["contacts"] = _contacts(contacts)
    px = source.get("px", {})
    for key, source_key in [
        ("official_statistics", "official-statistics"),
        ("discontinued", "discontinued"),
    ]:
        if isinstance(px.get(source_key), bool):
            shared[key] = px[source_key]
    if adapter == "pxweb_v1":
        shared.update(v1_listing_fields(record))
    else:
        base = (
            record["base_api_url"].rstrip("/")
            + "/tables/"
            + quote(record["dataset_code"], safe="")
        )
        language = quote(record["language"], safe="")
        shared.update(
            metadata_url=f"{base}/metadata?lang={language}&defaultSelection=false",
            data_url=f"{base}/data?lang={language}&outputFormat=json-stat2",
        )
        for key, native_key in [
            ("first_period", "firstPeriod"),
            ("last_period", "lastPeriod"),
            ("time_unit", "timeUnit"),
            ("description", "description"),
        ]:
            if catalogue.get(native_key):
                shared[key] = catalogue[native_key]
        if "time_unit" in shared:
            unit = str(shared.pop("time_unit")).lower()
            if unit in {
                "annual",
                "semiannual",
                "quarterly",
                "monthly",
                "weekly",
                "daily",
                "other",
            }:
                shared["time_unit"] = unit
        if catalogue.get("paths"):
            shared["paths"] = [
                [{k: node[k] for k in ("id", "label") if k in node} for node in path]
                for path in catalogue["paths"]
            ]
        if catalogue.get("subjectCode"):
            shared["subject"] = {"code": catalogue["subjectCode"]}
    if max_cells is not None:
        source["max_cells"] = max_cells
    for key in ("source_url", "doc_url", "metadata_url", "data_url"):
        if shared.get(key):
            shared[key] = _url(shared[key])
    result["extension"] = {"nordicintel": shared, adapter: source}
    for dimension in result["dimension"].values():
        extras = dimension.pop("extension", {})
        dim_shared = {}
        for old, new in [
            ("elimination", "elimination"),
            ("eliminationValueCode", "elimination_value"),
        ]:
            if old in extras:
                dim_shared[new] = extras.pop(old)
        for key in ("source", "updated"):
            if key in dimension:
                dim_shared[key] = dimension.pop(key)
        for code, unit in dimension.get("category", {}).get("unit", {}).items():
            if "base" in unit and "label" not in unit:
                unit["label"] = unit.pop("base")
            unknown = {
                k: unit.pop(k)
                for k in list(unit)
                if k not in {"label", "decimals", "symbol", "position"}
            }
            if unknown:
                extras.setdefault("categories", {}).setdefault(code, {})["unit"] = (
                    unknown
                )
        dimension["extension"] = {adapter: extras}
        if dim_shared:
            dimension["extension"]["nordicintel"] = dim_shared
        _links(dimension, adapter)
    _links(result, adapter)
    _retain_optional_source_fields(result, adapter)
    validate_metadata(result, adapter=adapter)
    return result

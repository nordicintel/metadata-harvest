"""The service registry: the three configuration files, loaded and validated once.

``load`` reads ``adapters.json``, ``organisations.json`` and ``catalogs.json`` from a
configuration directory, validates each against ``schema/source-configuration.schema.json``
and checks the cross-references. The result answers two questions: which service
bindings an adapter has, in configured order, and which organisation owns a binding.
Nothing else is derived here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import jsonschema

from apps.paths import CONFIG, SCHEMA

FILES = ("adapters.json", "organisations.json", "catalogs.json")
TYPES = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


@dataclass(frozen=True)
class Organisation:
    code: str
    id: str
    name: dict[str, list[str]]


@dataclass(frozen=True)
class ServiceBinding:
    """One configured service of one adapter.

    ``options`` is the adapter's defaults overlaid with the binding's own options.
    ``label`` is the title of the one catalog listing the service, or the endpoint
    host when the service is shared; ``description`` likewise, or empty.
    """

    id: str
    adapter: str
    source_code: str
    endpoint_url: str
    publisher: str | None
    order: int
    options: dict = field(default_factory=dict)
    label: str = ""
    description: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Registry:
    adapters: tuple[str, ...]
    organisations: dict[str, Organisation]
    services: tuple[ServiceBinding, ...]

    def bindings(self, adapter: str) -> list[ServiceBinding]:
        """The adapter's bindings in configured order."""
        if adapter not in self.adapters:
            raise ValueError(f"Unknown adapter: {adapter}")
        return sorted(
            (b for b in self.services if b.adapter == adapter), key=lambda b: b.order
        )

    def owner(self, binding: ServiceBinding) -> Organisation | None:
        return self.organisations.get(binding.publisher) if binding.publisher else None


def _read(root: Path, name: str, schema: dict) -> dict:
    document = json.loads((root / name).read_text(encoding="utf-8"))
    part = name.removesuffix(".json")
    try:
        jsonschema.validate(document, {"$ref": f"#/$defs/{part}", "$defs": schema})
    except jsonschema.ValidationError as error:
        raise ValueError(f"{name}: {error.message} at {error.json_path}") from error
    return document


def load(root: str | Path | None = None) -> Registry:
    """Load and validate one configuration directory (the repository's by default)."""
    root = Path(root) if root is not None else CONFIG
    if root.is_file():
        raise ValueError("The configuration path must be a directory")
    schema = json.loads(
        (SCHEMA / "source-configuration.schema.json").read_text(encoding="utf-8")
    )["$defs"]
    adapters = _read(root, FILES[0], schema)["adapters"]
    organisations = {
        code: Organisation(code, item["id"], item["name"])
        for code, item in _read(root, FILES[1], schema)["organisations"].items()
    }
    if len({o.id for o in organisations.values()}) != len(organisations):
        raise ValueError("Duplicate organisation identity")
    document = _read(root, FILES[2], schema)
    catalogs = {}
    for key, catalog in document["catalogs"].items():
        if key != catalog["id"]:
            raise ValueError(f"Catalog key must equal its identity: {key}")
        if catalog.get("publisher") not in organisations | {None: None}:
            raise ValueError(f"Unknown catalog organisation: {key}")
        for service in catalog["services"]:
            if service not in document["services"]:
                raise ValueError(f"Unknown catalog service: {key}")
            catalogs.setdefault(service, []).append(catalog)
    services, aliases, orders = [], set(), set()
    for key, item in document["services"].items():
        if key != item["id"]:
            raise ValueError(f"Service key must equal its identity: {key}")
        if item["adapter"] not in adapters:
            raise ValueError(f"Unknown service adapter: {item['adapter']}")
        if item.get("publisher") not in organisations | {None: None}:
            raise ValueError(f"Unknown service organisation: {key}")
        if (alias := (item["adapter"], item["source_code"])) in aliases:
            raise ValueError(f"Duplicate source alias: {alias}")
        if (order := (item["adapter"], item["order"])) in orders:
            raise ValueError(f"Duplicate adapter processing order: {order}")
        aliases.add(alias)
        orders.add(order)
        definition = adapters[item["adapter"]]
        options = definition.get("defaults", {}) | item.get("options", {})
        for name, parameter in definition.get("parameters", {}).items():
            if name not in options:
                if parameter.get("required"):
                    raise ValueError(f"Missing {item['adapter']} option: {name}")
            elif type(options[name]) not in TYPES[parameter["type"]]:
                raise ValueError(f"Invalid {item['adapter']} option: {name}")
        listed = catalogs.get(key, [])
        only = listed[0] if len(listed) == 1 else None
        services.append(
            ServiceBinding(
                id=key,
                adapter=item["adapter"],
                source_code=item["source_code"],
                endpoint_url=item["endpoint_url"],
                publisher=item.get("publisher"),
                order=item["order"],
                options=options,
                label=next(iter(only["title"].values()))[0]
                if only
                else urlsplit(item["endpoint_url"]).netloc,
                description={
                    lang: texts[0] for lang, texts in only["description"].items()
                }
                if only and only.get("description")
                else {},
            )
        )
    return Registry(tuple(adapters), organisations, tuple(services))

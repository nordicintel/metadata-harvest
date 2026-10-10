"""Resolve the three authoritative source files into metadata and worker settings.

Repository callers pass their configuration directory. Installed consumers use the
generated package copies; neither path depends on a different repository.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from metadata_harvest.models import LanguageMap, Organisation, Text
from metadata_harvest.registry import ADAPTERS

_active_configuration = ContextVar("source_configuration", default=None)


def activate_configuration(configuration):
    """Select a validated configuration for this worker's execution context."""
    _active_configuration.set(configuration)


@contextmanager
def use_configuration(configuration=None):
    token = _active_configuration.set(configuration)
    try:
        yield
    finally:
        _active_configuration.reset(token)


class ConfigurationModel(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class ParameterDefinition(ConfigurationModel):
    type: Literal["string", "number", "integer", "boolean", "array", "object"]
    required: bool = False
    description: str = ""


class AdapterDefinition(ConfigurationModel):
    title: LanguageMap
    description: LanguageMap
    uses_catalog_label: bool = False
    endpoint_parameter: Literal["base_url", "base_api_url"]
    parameters: dict[str, ParameterDefinition] = Field(default_factory=dict)
    defaults: dict[str, JsonValue] = Field(default_factory=dict)


class SourceScope(ConfigurationModel):
    contexts: list[str] = Field(default_factory=list)
    dataset_codes: list[str] = Field(default_factory=list)
    discovery_origins: list[str] = Field(default_factory=list)


class ServiceBinding(ConfigurationModel):
    id: Text
    adapter: Text
    source_code: Text
    endpoint_url: list[Text]
    publisher: str | None = None
    title: LanguageMap = Field(default_factory=dict)
    endpoint_description: list[str] = Field(default_factory=list)
    order: int = Field(ge=0)
    options: dict[str, JsonValue] = Field(default_factory=dict)
    # Identity preservation is operational, not an attribution assertion.
    identity_namespace: Text
    discovery_origins: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def usable_endpoints(self) -> Self:
        if not self.endpoint_url or any(
            urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc
            for url in self.endpoint_url
        ):
            raise ValueError("Service requires absolute HTTP(S) endpoints")
        return self


class CatalogConfiguration(ConfigurationModel):
    id: Text
    key: Text
    title: LanguageMap
    publisher: str | None = None
    services: list[Text] = Field(default_factory=list)
    description: LanguageMap = Field(default_factory=dict)
    homepage: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    scopes: dict[str, SourceScope] = Field(default_factory=dict)
    catalogs: list[str] = Field(default_factory=list)
    has_part: list[str] = Field(default_factory=list)


class SourceConfiguration(ConfigurationModel):
    configuration_version: Literal[1] = 1
    adapters: dict[str, AdapterDefinition]
    organisations: dict[str, Organisation]
    services: dict[str, ServiceBinding]
    catalogs: dict[str, CatalogConfiguration]

    @model_validator(mode="after")
    def resolve_references(self) -> Self:
        aliases, orders, organisation_ids = set(), set(), set()
        for code, organisation in self.organisations.items():
            if organisation.id in organisation_ids:
                raise ValueError(f"Duplicate organisation identity: {code}")
            organisation_ids.add(organisation.id)
        for adapter in self.adapters:
            if adapter not in ADAPTERS or not ADAPTERS[adapter].available:
                raise ValueError(f"Unknown adapter: {adapter}")
        for key, binding in self.services.items():
            if key != binding.id:
                raise ValueError("Service key must equal its stable identity")
            if binding.adapter not in self.adapters:
                raise ValueError(f"Unknown service adapter: {binding.adapter}")
            if (
                binding.publisher is not None
                and binding.publisher not in self.organisations
            ):
                raise ValueError(f"Unknown service organisation: {binding.publisher}")
            alias = (binding.adapter, binding.source_code)
            if alias in aliases:
                raise ValueError(f"Duplicate source alias: {alias}")
            aliases.add(alias)
            order = (binding.adapter, binding.order)
            if order in orders:
                raise ValueError(f"Duplicate adapter processing order: {order}")
            orders.add(order)
            definition = self.adapters[binding.adapter]
            options = definition.defaults | binding.options
            for name, parameter in definition.parameters.items():
                if name not in options:
                    if parameter.required:
                        raise ValueError(f"Missing {binding.adapter} option: {name}")
                    continue
                value = options[name]
                allowed = {
                    "string": (str,),
                    "number": (int, float),
                    "integer": (int,),
                    "boolean": (bool,),
                    "array": (list,),
                    "object": (dict,),
                }
                if type(value) not in allowed[parameter.type]:
                    raise ValueError(f"Invalid {binding.adapter} option: {name}")
        for key, catalog in self.catalogs.items():
            if key != catalog.id:
                raise ValueError("Catalog key must equal its stable identity")
            if (
                catalog.publisher is not None
                and catalog.publisher not in self.organisations
            ):
                raise ValueError(f"Unknown catalog organisation: {catalog.publisher}")
            if any(s not in self.services for s in catalog.services):
                raise ValueError(f"Unknown catalog service: {key}")
            if set(catalog.scopes) - set(catalog.services):
                raise ValueError(f"Scope has no catalog service: {key}")
            if any(c not in self.catalogs for c in catalog.catalogs + catalog.has_part):
                raise ValueError(f"Unknown related catalog: {key}")
        return self


def load_configuration(path: str | Path | None = None) -> SourceConfiguration:
    """Validate an explicit directory or the packaged three-file snapshot."""
    if path is None:
        return _active_configuration.get() or _packaged()
    root = Path(path)
    if root.is_file():
        raise ValueError("--config must name the directory containing the three files")
    return _load(root)


def _load(root) -> SourceConfiguration:
    values = {}
    for filename, fields in (
        ("adapters.json", {"adapters"}),
        ("organisations.json", {"organisations"}),
        ("catalogs.json", {"services", "catalogs"}),
    ):
        document = json.loads(root.joinpath(filename).read_text(encoding="utf-8"))
        if document.get("configuration_version") != 1 or set(document) != fields | {
            "configuration_version"
        }:
            raise ValueError(f"Invalid configuration envelope: {filename}")
        values.update({k: document[k] for k in fields})
    return SourceConfiguration.model_validate(values)


@cache
def _packaged() -> SourceConfiguration:
    return _load(files("metadata_harvest").joinpath("configuration"))


def providers_for_adapter(
    adapter: str,
    *,
    configuration: SourceConfiguration | None = None,
    legacy_path: str | Path | None = None,
) -> list[dict]:
    """Project validated settings to the existing worker dictionaries.

    ``legacy_path`` is an explicit transition override, never an implicit fallback.
    Values and source ordering are preserved; only metadata ownership uses the
    organisation references. No caller is given a mutable cached configuration.
    """
    if legacy_path is not None:
        rows = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(
            not isinstance(r, dict) or not r.get("code") for r in rows
        ):
            raise ValueError("Legacy providers must be a list of source configurations")
        if len({r["code"] for r in rows}) != len(rows):
            raise ValueError("Duplicate legacy source alias")
        return rows
    configuration = configuration or load_configuration()
    if adapter not in configuration.adapters:
        raise ValueError(f"Unknown adapter: {adapter}")
    definition = configuration.adapters[adapter]
    rows = []
    for binding in sorted(configuration.services.values(), key=lambda s: s.order):
        if binding.adapter != adapter:
            continue
        row = {
            "code": binding.source_code,
            definition.endpoint_parameter: binding.endpoint_url[0],
        }
        row.update(definition.defaults | binding.options)
        catalogs = [
            c for c in configuration.catalogs.values() if binding.id in c.services
        ]
        if definition.uses_catalog_label:
            row["label"] = (
                next(iter(catalogs[0].title.values()))[0]
                if len(catalogs) == 1
                else urlsplit(binding.endpoint_url[0]).netloc
            )
        if len(catalogs) == 1 and catalogs[0].description:
            row["description"] = {
                lang: texts[0] for lang, texts in catalogs[0].description.items()
            }
        rows.append(json.loads(json.dumps(row, ensure_ascii=False)))
    return rows


def legacy_configuration(
    configuration: SourceConfiguration, adapter: str, rows: list[dict]
) -> SourceConfiguration:
    """Apply explicit transition overrides to service metadata as well as workers."""
    document = configuration.model_dump()
    endpoint_key = configuration.adapters[adapter].endpoint_parameter
    existing = {
        b.source_code: b
        for b in configuration.services.values()
        if b.adapter == adapter
    }
    next_order = max((b.order for b in existing.values()), default=-1) + 1
    for row in rows:
        code = row["code"]
        previous = existing.get(code)
        identity = (
            previous.id
            if previous
            else f"urn:nordicintel:service:custom:{adapter}:{code}"
        )
        binding = (
            previous.model_dump()
            if previous
            else {
                "id": identity,
                "adapter": adapter,
                "source_code": code,
                "identity_namespace": code,
                "order": next_order,
            }
        )
        if not previous:
            next_order += 1
        binding["endpoint_url"] = [row[endpoint_key]]
        binding["options"] = {
            k: v for k, v in row.items() if k not in {"code", endpoint_key}
        }
        document["services"][identity] = binding
    return SourceConfiguration.model_validate(document)

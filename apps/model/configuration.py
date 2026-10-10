"""Typed source configuration and in-memory reference validation."""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from apps.model.models import LanguageMap, Organisation, Text


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

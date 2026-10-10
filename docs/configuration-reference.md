# Configuration model reference

Generated from the configuration Pydantic models; do not edit by hand.

Configuration fields are internal construction values. Organisation vocabulary aliases are standard RDF properties; published resource mappings are described in [configuration.md](configuration.md). JSON-valued extensions are accepted without changing these models.

## AdapterDefinition

| Field | Required | Definition |
| --- | --- | --- |
| `title` | True | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Title", "type": "object"}` |
| `description` | True | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Description", "type": "object"}` |
| `uses_catalog_label` | False | `{"default": false, "title": "Uses Catalog Label", "type": "boolean"}` |
| `endpoint_parameter` | True | `{"enum": ["base_url", "base_api_url"], "title": "Endpoint Parameter", "type": "string"}` |
| `parameters` | False | `{"additionalProperties": {"$ref": "#/$defs/ParameterDefinition"}, "title": "Parameters", "type": "object"}` |
| `defaults` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Defaults", "type": "object"}` |

## CatalogConfiguration

| Field | Required | Definition |
| --- | --- | --- |
| `id` | True | `{"minLength": 1, "title": "Id", "type": "string"}` |
| `key` | True | `{"minLength": 1, "title": "Key", "type": "string"}` |
| `title` | True | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Title", "type": "object"}` |
| `publisher` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Publisher"}` |
| `services` | False | `{"items": {"minLength": 1, "type": "string"}, "title": "Services", "type": "array"}` |
| `description` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Description", "type": "object"}` |
| `homepage` | False | `{"items": {"type": "string"}, "title": "Homepage", "type": "array"}` |
| `languages` | False | `{"items": {"type": "string"}, "title": "Languages", "type": "array"}` |
| `scopes` | False | `{"additionalProperties": {"$ref": "#/$defs/SourceScope"}, "title": "Scopes", "type": "object"}` |
| `catalogs` | False | `{"items": {"type": "string"}, "title": "Catalogs", "type": "array"}` |
| `has_part` | False | `{"items": {"type": "string"}, "title": "Has Part", "type": "array"}` |

## Organisation

| Field | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "http://www.w3.org/ns/org#Organization", "default": "http://www.w3.org/ns/org#Organization", "title": "@Type", "type": "string"}` |
| `foaf:name` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Foaf:Name", "type": "object"}` |
| `foaf:mbox` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Foaf:Mbox"}` |
| `foaf:homepage` | False | `{"items": {"type": "string"}, "title": "Foaf:Homepage", "type": "array"}` |
| `dct:identifier` | False | `{"items": {"type": "string"}, "title": "Dct:Identifier", "type": "array"}` |

## ParameterDefinition

| Field | Required | Definition |
| --- | --- | --- |
| `type` | True | `{"enum": ["string", "number", "integer", "boolean", "array", "object"], "title": "Type", "type": "string"}` |
| `required` | False | `{"default": false, "title": "Required", "type": "boolean"}` |
| `description` | False | `{"default": "", "title": "Description", "type": "string"}` |

## ServiceBinding

| Field | Required | Definition |
| --- | --- | --- |
| `id` | True | `{"minLength": 1, "title": "Id", "type": "string"}` |
| `adapter` | True | `{"minLength": 1, "title": "Adapter", "type": "string"}` |
| `source_code` | True | `{"minLength": 1, "title": "Source Code", "type": "string"}` |
| `endpoint_url` | True | `{"items": {"minLength": 1, "type": "string"}, "title": "Endpoint Url", "type": "array"}` |
| `publisher` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Publisher"}` |
| `title` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Title", "type": "object"}` |
| `endpoint_description` | False | `{"items": {"type": "string"}, "title": "Endpoint Description", "type": "array"}` |
| `order` | True | `{"minimum": 0, "title": "Order", "type": "integer"}` |
| `options` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Options", "type": "object"}` |
| `identity_namespace` | True | `{"minLength": 1, "title": "Identity Namespace", "type": "string"}` |
| `discovery_origins` | False | `{"items": {"type": "string"}, "title": "Discovery Origins", "type": "array"}` |

## SourceConfiguration

| Field | Required | Definition |
| --- | --- | --- |
| `configuration_version` | False | `{"const": 1, "default": 1, "title": "Configuration Version", "type": "integer"}` |
| `adapters` | True | `{"additionalProperties": {"$ref": "#/$defs/AdapterDefinition"}, "title": "Adapters", "type": "object"}` |
| `organisations` | True | `{"additionalProperties": {"$ref": "#/$defs/Organisation"}, "title": "Organisations", "type": "object"}` |
| `services` | True | `{"additionalProperties": {"$ref": "#/$defs/ServiceBinding"}, "title": "Services", "type": "object"}` |
| `catalogs` | True | `{"additionalProperties": {"$ref": "#/$defs/CatalogConfiguration"}, "title": "Catalogs", "type": "object"}` |

## SourceScope

| Field | Required | Definition |
| --- | --- | --- |
| `contexts` | False | `{"items": {"type": "string"}, "title": "Contexts", "type": "array"}` |
| `dataset_codes` | False | `{"items": {"type": "string"}, "title": "Dataset Codes", "type": "array"}` |
| `discovery_origins` | False | `{"items": {"type": "string"}, "title": "Discovery Origins", "type": "array"}` |

# Metadata model reference

Generated from [Pydantic models](../../apps/model/models.py); do not edit by hand.

NordicIntel profile 2.0.0 is based on StatDCAT-AP 3.0.0. Vocabulary aliases are the serialized property names. Descriptive completeness is diagnostic; retrieval identities and structures are validated.

Every source-bearing model accepts JSON-valued extras. Serialization places unfamiliar properties in `ni:additionalAttributes` under the `custom` namespace; adapters use their source namespace. This property is explicitly JSON-valued in the inline JSON-LD context.

## AdapterOptions

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `max_cells` | False | `{"anyOf": [{"exclusiveMinimum": 0, "type": "integer"}, {"type": "null"}], "default": null, "title": "Max Cells"}` |
| `scope` | False | `{"anyOf": [{"enum": ["municipality", "ou"], "type": "string"}, {"type": "null"}], "default": null, "title": "Scope"}` |
| `kpi_id` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Kpi Id"}` |

## Agent

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "@Id"}` |
| `@type` | False | `{"default": "foaf:Agent", "enum": ["foaf:Agent", "http://www.w3.org/ns/org#Organization"], "title": "@Type", "type": "string"}` |
| `foaf:name` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Foaf:Name", "type": "object"}` |
| `foaf:mbox` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Foaf:Mbox"}` |
| `foaf:homepage` | False | `{"items": {"type": "string"}, "title": "Foaf:Homepage", "type": "array"}` |
| `dct:identifier` | False | `{"items": {"type": "string"}, "title": "Dct:Identifier", "type": "array"}` |

## Attribute

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "qb:AttributeProperty", "default": "qb:AttributeProperty", "title": "@Type", "type": "string"}` |
| `skos:prefLabel` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Preflabel", "type": "object"}` |

## Catalog

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "dcat:Catalog", "default": "dcat:Catalog", "title": "@Type", "type": "string"}` |
| `dct:title` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Title", "type": "object"}` |
| `dct:publisher` | False | `{"items": {"$ref": "#/$defs/Agent"}, "title": "Dct:Publisher", "type": "array"}` |
| `dct:description` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Description", "type": "object"}` |
| `foaf:homepage` | False | `{"items": {"type": "string"}, "title": "Foaf:Homepage", "type": "array"}` |
| `dct:language` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dct:Language", "type": "array"}` |
| `dcat:dataset` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dcat:Dataset", "type": "array"}` |
| `dcat:service` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dcat:Service", "type": "array"}` |
| `dcat:catalog` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dcat:Catalog", "type": "array"}` |
| `dct:hasPart` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dct:Haspart", "type": "array"}` |
| `dct:creator` | False | `{"items": {"$ref": "#/$defs/Agent"}, "title": "Dct:Creator", "type": "array"}` |
| `dct:license` | False | `{"items": {"type": "string"}, "title": "Dct:License", "type": "array"}` |
| `dct:modified` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dct:Modified"}` |

## CodeList

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "skos:ConceptScheme", "default": "skos:ConceptScheme", "title": "@Type", "type": "string"}` |
| `skos:prefLabel` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Preflabel", "type": "object"}` |
| `codes` | False | `{"items": {"$ref": "#/$defs/Concept"}, "title": "Codes", "type": "array"}` |
| `ni:categoryOrder` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Ni:Categoryorder", "type": "object"}` |
| `ni:sourceIdentity` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Ni:Sourceidentity", "type": "object"}` |

## Component

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `qb:dimension` | True | `{"$ref": "#/$defs/Dimension"}` |
| `qb:order` | True | `{"minimum": 1, "title": "Qb:Order", "type": "integer"}` |

## Concept

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "@Id"}` |
| `@type` | False | `{"const": "skos:Concept", "default": "skos:Concept", "title": "@Type", "type": "string"}` |
| `skos:notation` | True | `{"minLength": 1, "title": "Skos:Notation", "type": "string"}` |
| `skos:prefLabel` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Preflabel", "type": "object"}` |
| `skos:note` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Note", "type": "object"}` |
| `skos:broader` | False | `{"items": {"type": "string"}, "title": "Skos:Broader", "type": "array"}` |
| `ni:unit` | False | `{"anyOf": [{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, {"type": "null"}], "default": null, "title": "Ni:Unit"}` |
| `ni:coordinates` | False | `{"$ref": "#/$defs/JsonValue", "default": null}` |
| `ni:aggregationMap` | False | `{"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": null, "title": "Ni:Aggregationmap"}` |

## ContactPoint

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@type` | False | `{"const": "vcard:Kind", "default": "vcard:Kind", "title": "@Type", "type": "string"}` |
| `vcard:fn` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Vcard:Fn"}` |
| `vcard:hasEmail` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Vcard:Hasemail"}` |
| `vcard:hasTelephone` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Vcard:Hastelephone"}` |
| `vcard:organization-name` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Vcard:Organization-Name"}` |
| `vcard:hasAddress` | False | `{"$ref": "#/$defs/JsonValue", "default": null}` |
| `vcard:hasURL` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Vcard:Hasurl"}` |

## DataService

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `ni:adapter` | True | `{"minLength": 1, "title": "Ni:Adapter", "type": "string"}` |
| `dct:publisher` | False | `{"items": {"$ref": "#/$defs/Agent"}, "title": "Dct:Publisher", "type": "array"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "dcat:DataService", "default": "dcat:DataService", "title": "@Type", "type": "string"}` |
| `dct:title` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Title", "type": "object"}` |
| `dct:description` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Description", "type": "object"}` |
| `dcat:endpointURL` | False | `{"items": {"type": "string"}, "title": "Dcat:Endpointurl", "type": "array"}` |
| `dcat:endpointDescription` | False | `{"items": {"type": "string"}, "title": "Dcat:Endpointdescription", "type": "array"}` |
| `dcat:servesDataset` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dcat:Servesdataset", "type": "array"}` |
| `foaf:page` | False | `{"items": {"type": "string"}, "title": "Foaf:Page", "type": "array"}` |
| `dcat:landingPage` | False | `{"items": {"type": "string"}, "title": "Dcat:Landingpage", "type": "array"}` |
| `dcat:contactPoint` | False | `{"items": {"$ref": "#/$defs/ContactPoint"}, "title": "Dcat:Contactpoint", "type": "array"}` |
| `dct:conformsTo` | False | `{"items": {"$ref": "#/$defs/Reference"}, "title": "Dct:Conformsto", "type": "array"}` |

## DataStructureDefinition

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "qb:DataStructureDefinition", "default": "qb:DataStructureDefinition", "title": "@Type", "type": "string"}` |
| `qb:component` | False | `{"items": {"$ref": "#/$defs/Component"}, "title": "Qb:Component", "type": "array"}` |

## Dataset

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `dct:issued` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dct:Issued"}` |
| `adms:status` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Adms:Status", "type": "array"}` |
| `dct:language` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Language", "type": "array"}` |
| `dct:conformsTo` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Conformsto", "type": "array"}` |
| `dct:type` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Type", "type": "array"}` |
| `dct:rights` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Rights", "type": "array"}` |
| `dct:accessRights` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Accessrights", "type": "array"}` |
| `dcatap:applicableLegislation` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dcatap:Applicablelegislation", "type": "array"}` |
| `dcat:spatialResolutionInMeters` | False | `{"items": {"type": "number"}, "title": "Dcat:Spatialresolutioninmeters", "type": "array"}` |
| `ni:sourceAssertions` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Ni:Sourceassertions", "type": "object"}` |
| `dct:creator` | False | `{"items": {"$ref": "#/$defs/Agent"}, "title": "Dct:Creator", "type": "array"}` |
| `dct:accrualPeriodicity` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Accrualperiodicity", "type": "array"}` |
| `dct:spatial` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Spatial", "type": "array"}` |
| `dct:provenance` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Provenance", "type": "array"}` |
| `dct:relation` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Relation", "type": "array"}` |
| `dct:source` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Source", "type": "array"}` |
| `dcat:inSeries` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dcat:Inseries", "type": "array"}` |
| `dct:isReferencedBy` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Isreferencedby", "type": "array"}` |
| `dcat:version` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dcat:Version"}` |
| `adms:versionNotes` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Adms:Versionnotes", "type": "object"}` |
| `catalogs` | False | `{"items": {"$ref": "#/$defs/Catalog"}, "title": "Catalogs", "type": "array"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "dcat:Dataset", "default": "dcat:Dataset", "title": "@Type", "type": "string"}` |
| `ni:profileVersion` | False | `{"const": "2.0.0", "default": "2.0.0", "title": "Ni:Profileversion", "type": "string"}` |
| `dct:title` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Title", "type": "object"}` |
| `dct:description` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Description", "type": "object"}` |
| `dct:identifier` | False | `{"items": {"type": "string"}, "title": "Dct:Identifier", "type": "array"}` |
| `dct:modified` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dct:Modified"}` |
| `dct:temporal` | False | `{"items": {"$ref": "#/$defs/PeriodOfTime"}, "title": "Dct:Temporal", "type": "array"}` |
| `dcat:temporalResolution` | False | `{"items": {"type": "string"}, "title": "Dcat:Temporalresolution", "type": "array"}` |
| `dcat:landingPage` | False | `{"items": {"type": "string"}, "title": "Dcat:Landingpage", "type": "array"}` |
| `foaf:page` | False | `{"items": {"type": "string"}, "title": "Foaf:Page", "type": "array"}` |
| `dct:publisher` | False | `{"items": {"$ref": "#/$defs/Agent"}, "title": "Dct:Publisher", "type": "array"}` |
| `dcat:contactPoint` | False | `{"items": {"$ref": "#/$defs/ContactPoint"}, "title": "Dcat:Contactpoint", "type": "array"}` |
| `dcat:theme` | False | `{"items": {"$ref": "#/$defs/Concept"}, "title": "Dcat:Theme", "type": "array"}` |
| `dcat:keyword` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dcat:Keyword", "type": "object"}` |
| `skos:note` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Note", "type": "object"}` |
| `statdcatap:dimension` | False | `{"items": {"anyOf": [{"$ref": "#/$defs/Dimension"}, {"$ref": "#/$defs/Reference"}]}, "title": "Statdcatap:Dimension", "type": "array"}` |
| `statdcatap:measure` | False | `{"items": {"$ref": "#/$defs/Measure"}, "title": "Statdcatap:Measure", "type": "array"}` |
| `statdcatap:attribute` | False | `{"items": {"$ref": "#/$defs/Attribute"}, "title": "Statdcatap:Attribute", "type": "array"}` |
| `statdcatap:dataStructureDefinition` | False | `{"anyOf": [{"$ref": "#/$defs/Reference"}, {"$ref": "#/$defs/DataStructureDefinition"}, {"type": "null"}], "default": null, "title": "Statdcatap:Datastructuredefinition"}` |
| `dcat:distribution` | False | `{"items": {"$ref": "#/$defs/Distribution"}, "title": "Dcat:Distribution", "type": "array"}` |
| `ni:officialStatistics` | False | `{"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": null, "title": "Ni:Officialstatistics"}` |
| `ni:discontinued` | False | `{"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": null, "title": "Ni:Discontinued"}` |
| `ni:nextRelease` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Ni:Nextrelease"}` |
| `ni:sourceEvidence` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Ni:Sourceevidence", "type": "object"}` |
| `ni:matchEvidence` | False | `{"items": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Matchevidence", "type": "array"}` |

## Dimension

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "@Id"}` |
| `@type` | False | `{"const": "qb:DimensionProperty", "default": "qb:DimensionProperty", "title": "@Type", "type": "string"}` |
| `dct:identifier` | True | `{"minLength": 1, "title": "Dct:Identifier", "type": "string"}` |
| `skos:prefLabel` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Preflabel", "type": "object"}` |
| `skos:note` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Note", "type": "object"}` |
| `qb:codeList` | False | `{"anyOf": [{"$ref": "#/$defs/CodeList"}, {"type": "null"}], "default": null}` |
| `ni:role` | False | `{"anyOf": [{"enum": ["time", "geo", "metric"], "type": "string"}, {"type": "null"}], "default": null, "title": "Ni:Role"}` |
| `ni:elimination` | False | `{"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": null, "title": "Ni:Elimination"}` |
| `ni:eliminationValue` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Ni:Eliminationvalue"}` |
| `ni:links` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Ni:Links", "type": "object"}` |

## Distribution

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `dct:issued` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dct:Issued"}` |
| `adms:status` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Adms:Status", "type": "array"}` |
| `dct:language` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Language", "type": "array"}` |
| `dct:conformsTo` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Conformsto", "type": "array"}` |
| `dct:type` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Type", "type": "array"}` |
| `dct:rights` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Rights", "type": "array"}` |
| `dct:accessRights` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Accessrights", "type": "array"}` |
| `dcatap:applicableLegislation` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dcatap:Applicablelegislation", "type": "array"}` |
| `dcat:spatialResolutionInMeters` | False | `{"items": {"type": "number"}, "title": "Dcat:Spatialresolutioninmeters", "type": "array"}` |
| `ni:sourceAssertions` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Ni:Sourceassertions", "type": "object"}` |
| `dct:format` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dct:Format", "type": "array"}` |
| `dcat:mediaType` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dcat:Mediatype", "type": "array"}` |
| `dcatap:availability` | False | `{"items": {"$ref": "#/$defs/VocabularyResource"}, "title": "Dcatap:Availability", "type": "array"}` |
| `dcat:byteSize` | False | `{"anyOf": [{"minimum": 0, "type": "number"}, {"type": "null"}], "default": null, "title": "Dcat:Bytesize"}` |
| `dct:modified` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dct:Modified"}` |
| `skos:note` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Note", "type": "object"}` |
| `dcat:temporalResolution` | False | `{"items": {"type": "string"}, "title": "Dcat:Temporalresolution", "type": "array"}` |
| `dcat:landingPage` | False | `{"items": {"type": "string"}, "title": "Dcat:Landingpage", "type": "array"}` |
| `foaf:page` | False | `{"items": {"type": "string"}, "title": "Foaf:Page", "type": "array"}` |
| `dcat:contactPoint` | False | `{"items": {"$ref": "#/$defs/ContactPoint"}, "title": "Dcat:Contactpoint", "type": "array"}` |
| `dcat:theme` | False | `{"items": {"$ref": "#/$defs/Concept"}, "title": "Dcat:Theme", "type": "array"}` |
| `ni:nextRelease` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Ni:Nextrelease"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "dcat:Distribution", "default": "dcat:Distribution", "title": "@Type", "type": "string"}` |
| `dct:title` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Title", "type": "object"}` |
| `dct:description` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Dct:Description", "type": "object"}` |
| `dcat:accessURL` | False | `{"items": {"type": "string"}, "title": "Dcat:Accessurl", "type": "array"}` |
| `dcat:downloadURL` | False | `{"items": {"type": "string"}, "title": "Dcat:Downloadurl", "type": "array"}` |
| `dct:license` | False | `{"items": {"type": "string"}, "title": "Dct:License", "type": "array"}` |
| `dcat:accessService` | False | `{"items": {"$ref": "#/$defs/DataService"}, "title": "Dcat:Accessservice", "type": "array"}` |
| `ni:structure` | False | `{"anyOf": [{"$ref": "#/$defs/DataStructureDefinition"}, {"type": "null"}], "default": null}` |
| `ni:retrieval` | False | `{"anyOf": [{"$ref": "#/$defs/RetrievalBinding"}, {"type": "null"}], "default": null}` |
| `dct:temporal` | False | `{"items": {"$ref": "#/$defs/PeriodOfTime"}, "title": "Dct:Temporal", "type": "array"}` |
| `ni:officialStatistics` | False | `{"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": null, "title": "Ni:Officialstatistics"}` |
| `ni:discontinued` | False | `{"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": null, "title": "Ni:Discontinued"}` |
| `ni:sourceEvidence` | False | `{"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "title": "Ni:Sourceevidence", "type": "object"}` |

## Measure

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |
| `@type` | False | `{"const": "qb:MeasureProperty", "default": "qb:MeasureProperty", "title": "@Type", "type": "string"}` |
| `skos:prefLabel` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Skos:Preflabel", "type": "object"}` |

## PeriodOfTime

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@type` | False | `{"const": "dct:PeriodOfTime", "default": "dct:PeriodOfTime", "title": "@Type", "type": "string"}` |
| `dcat:startDate` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dcat:Startdate"}` |
| `dcat:endDate` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Dcat:Enddate"}` |

## QueryDimension

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `code` | True | `{"minLength": 1, "title": "Code", "type": "string"}` |
| `categories` | True | `{"items": {"minLength": 1, "type": "string"}, "title": "Categories", "type": "array"}` |
| `index_encoding` | False | `{"default": "list", "enum": ["list", "dict", "omitted"], "title": "Index Encoding", "type": "string"}` |

## Reference

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | True | `{"minLength": 1, "title": "@Id", "type": "string"}` |

## RetrievalBinding

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `source` | True | `{"$ref": "#/$defs/SourceIdentity"}` |
| `dimensions` | True | `{"items": {"$ref": "#/$defs/QueryDimension"}, "title": "Dimensions", "type": "array"}` |
| `metadata_url` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Metadata Url"}` |
| `options` | False | `{"$ref": "#/$defs/AdapterOptions"}` |

## SourceIdentity

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `adapter` | True | `{"minLength": 1, "title": "Adapter", "type": "string"}` |
| `source_code` | True | `{"minLength": 1, "title": "Source Code", "type": "string"}` |
| `dataset_code` | True | `{"minLength": 1, "title": "Dataset Code", "type": "string"}` |
| `language` | True | `{"minLength": 1, "title": "Language", "type": "string"}` |

## VocabularyResource

| Property | Required | Definition |
| --- | --- | --- |
| `ni:additionalAttributes` | False | `{"additionalProperties": {"additionalProperties": {"$ref": "#/$defs/JsonValue"}, "type": "object"}, "title": "Ni:Additionalattributes", "type": "object"}` |
| `@id` | False | `{"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "@Id"}` |
| `rdfs:label` | False | `{"additionalProperties": {"items": {"type": "string"}, "type": "array"}, "title": "Rdfs:Label", "type": "object"}` |
| `dct:identifier` | False | `{"items": {"type": "string"}, "title": "Dct:Identifier", "type": "array"}` |

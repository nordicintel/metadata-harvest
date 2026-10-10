# Extensible StatDCAT metadata contract

Local [Pydantic v2 models](../../apps/model/models.py) define NordicIntel profile **2.0.0**. The independently pinned upstream basis is [StatDCAT-AP 3.0.0](https://semiceu.github.io/StatDCAT-AP/drafts/3.0.0/); its exact commit and document hash are in [profile.json](../../config/profile.json). This is an extensible profile, not a promise of exact upstream shape conformance.

Generate JSON Schema and [field documentation](reference.md) with `uv run python scripts/generate_schema_reference.py`; CI checks both against the models. The explicitly named legacy schema remains only for native parsing and worker representation and explicit historical readers.

## Identity and structure

A dataset combines language-tagged metadata and independently replaceable source distributions. Kolada municipality and OU contributions share the native KPI dataset identity. Each distribution retains its scope, language, selectable codes, category order, structure and operational options. Conflicting structural versions are not forced into a common shape.

Organisations can publish catalogs, datasets and services; each resource declares its publisher independently. Every distribution's `dcat:accessService` identifies its concrete configured API service. Twelve shared adapter definitions supply construction defaults. [The complete inventory](../configuration/inventory.md) maps configured catalogs, publishers, services and original routing aliases. Göteborg, Linköping, Västra Götalandsregionen and Länsstyrelserna each publish both EntryScape and PXWeb catalogs.

`ni:retrieval.source` retains the adapter, original routing alias as `source_code`, dataset identifier and language. The `provider_code` Python compatibility property preserves existing operational callers. Portal matching enriches this identity; it never renames or merges datasets solely because they share a portal entry. Internal URNs preserve opaque codes by reversible percent encoding.

Dataset-level structural properties use references to embedded RDF Data Cube definitions when applicable. Distribution structures remain authoritative. SKOS concept schemes contain category labels, notes, units, coordinates, hierarchies, aggregation mappings and source extensions. `ni:retrieval.dimensions` preserves selectable codes and ordering independently of RDF graph order. Alternate aggregation lists remain descriptive.

Codelists preserve language-specific sequences in `ni:categoryOrder`; merging translated labels does not discard different source ordering. Conflicting unit, mapping or extension assertions remain separate, JSON-compatible evidence.

## Field dispositions

| Previous attribute | Current location |
| --- | --- |
| `label`, description | `dct:title`, `dct:description`, language-tagged |
| `updated` | `dct:modified` (source data modification) |
| `first_period`, `last_period` | `dct:temporal` with `dcat:startDate` and `dcat:endDate` where conversion is faithful |
| `time_unit` | `dcat:temporalResolution` when representable; never publication frequency |
| `source_url`, `doc_url` | `dcat:landingPage`, `foaf:page` |
| `data_url` | Distribution `dcat:accessURL`; downloaded resources use `dcat:downloadURL` |
| `metadata_url`, adapter request options | `ni:retrieval` |
| contacts, subject | `dcat:contactPoint`, `dcat:theme` |
| publisher, creator, language, licence, frequency, lifecycle status | Standard properties at their source level; licences stay with their distributions |
| dimension IDs, membership, order | RDF Data Cube components and explicit retrieval order |
| category code, label, note, hierarchy | SKOS notation, preferred label, note and broader relations |
| units, coordinates, aggregation maps | Typed concept properties in the NordicIntel namespace |
| official-statistics and discontinued flags | Strict `bool | None` properties `ni:officialStatistics`, `ni:discontinued` |
| elimination behaviour | Typed dimension `ni:elimination`, `ni:eliminationValue` |
| other root attributes and original source assertions | `ni:sourceEvidence` at the source distribution |
| otherwise unmapped fields at any supported level | `ni:additionalAttributes` under a source namespace |

Successfully mapped fields are not kept as parallel custom fields. Original period notation is represented by a notation descriptor where needed for exact worker reconstruction; genuinely unrepresentable periods stay as source attributes. Empty-value representation and source field presence remain explicit when the old format would otherwise lose them.

Every source-bearing model uses `extra="allow"` with JSON-valued validation. Unfamiliar Python/model fields serialize into `ni:additionalAttributes.custom`; adapters use their own namespace. The inline JSON-LD context types arbitrary JSON containers explicitly as `@json`. Future nested values, zeroes, `False`, empty strings and opaque codes survive serialization.

Invalid identities, membership, category order, hierarchy and unusable retrieval bindings fail validation. Optional source anomalies are retained with diagnostic evidence. Missing descriptive fields are quality findings, not publication rejection. Network requests, matching, merging and persistence remain outside validators.

## Accepted collections and publication

Accepted `data/<adapter>/metadata/*.jsonl.gz` collections are grouped by canonical provider and contain multilingual models. Task keys, checkpoints and retries still use original source identities; one failed contribution must not erase a successful sibling. Public output is exactly:

| Archive | Member |
| --- | --- |
| `data/catalog/catalog.7z` | `catalog.jsonl`, one assembled dataset per line |
| `data/catalog/codelists.7z` | `codelists.jsonl`, one SKOS scheme per line |

Consumers use [catalog distribution retrieval](../retrieval/README.md) or call a retrieval adapter directly. Harvesting exposes no consumer retrieval entry points.

## Offline migration and recovery

`uv run python -m apps.migrations.collections --source data --output output/statdcat-migration/data` builds a separate candidate without reharvesting. Per-adapter gzip ledgers connect every original record to its dataset/distribution and record every attribute disposition, source hash and exact round-trip result. Operational records are copied unchanged. Source files are never replaced by this command.

Cutover must include code, collections, generated schemas, catalog state and documentation together. Worker result markers carry the profile version; incompatible pre-cutover artifacts are rejected. Git history and `convert_collections` retain recovery for earlier collection layouts. Catalog assembly validates candidates and records accepted committed Git blob identities for failed-input reconstruction.

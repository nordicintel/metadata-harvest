# Integration ownership and architecture

`metadata-harvest` owns API integration code, source selection, organisation, catalog and service configuration,
metadata semantics, extensible StatDCAT Pydantic models, observation-result schemas, validation,
shared file-catalog construction and enrichment interfaces,
independent harvesting jobs, on-demand retrieval and integration documentation.

The catalogue service, website, public HTTP API, production databases, file scrapers
and all BRÅ work remain external. Consumers may use the shared contract for file-backed
observations; this repository adds no file ingestion. UKÄ's CSV exports are read
transiently, only to derive complete metadata, and are never stored. Trafikanalys
product documentation pages are read for API combinations until that API is retired.
EntryScape RowStore tables are discovered through EntryStore search and read, when
larger than one page, from the CSV files they were built from; only their structure
is kept. Existing PXWeb v1 website
discovery is retained explicitly because replacing it would change coverage.

## Boundaries

- `adapters/pxweb_v1/` and `adapters/pxweb_v2/` own source discovery, requests, native
  validation and retrieval protocol construction; v1 database/location assumptions
  do not leak into v2 discovery.
- `models.py` is the authoritative Pydantic v2 metadata contract; JSON Schema and
  field documentation are generated from it. `profile.json` pins upstream StatDCAT
  and versions the NordicIntel profile separately.
- `statdcat.py` converts native metadata into typed contributions and assembles
  multilingual datasets, independent distributions and SKOS codelists.
  `mapping.py`, `validation.py` and `semantics.py` retain native/legacy parsing.
- `configuration.py` validates and resolves the three authoritative configuration files.
  `sources.py` resolves resource-specific publishers independently of operational routing
  aliases. Concrete DataServices identify configured APIs and reference shared adapter definitions.
- `accepted.py` persists multilingual collections under their stable grouping identities and reconstructs
  independent native task records for transition workers.
- `retrieval.py` assembles protocol responses by coordinates; only the caller's
  `aiohttp` session performs requests, with caller-owned rate limits and retries.
- `storage.py` owns deterministic grouped serialization and atomic replacements;
  `artifacts.py` owns neutral artifact hashes, publication boundaries, size gates,
  checkpoint paths and outcome classification.
- `workflow.py` retains v1 reconciliation; v2 has its own orchestration, including
  independent codelist retries; `registry.py` explicitly identifies available adapters.
- `adapters/kolada/` owns native change probes, targeted scope updates and retrieval.
  It uses its own current files and Kolada API v3. Its initial trusted documents
  receive sample-based verification, not repeated corpus validation.
- `runner.py` preserves existing commands and v1 defaults; Actions collectors are
  the only production writers, sharing the `production-publication` collector
  queue. Adapter harvesting queues are independent.
- `catalog_assembly.py` groups accepted contributions, matches the local portal
  snapshot, enriches datasets, applies publication filters and validates references.
  Accepted Git blob identities recover failed current inputs; missing collections
  do not prove deletion. Fingerprints and verified output ranges support reuse.
- `portal.py` owns snapshot import/refresh and conservative matching. Unmatched
  harvested datasets remain included; portal-only datasets are not added.
- `catalog.py` packages `catalog.jsonl` and `codelists.jsonl` into their respective
  7z archives and promotes them with catalog state/reports into Git. Catalog builds
  are offline and never dispatch harvesting or portal refresh.
- `processing/` defines content-enrichment then semantic-enrichment proposals;
  no external lookup, inference rule or AI processor is implemented yet.
- `publication.py` owns scoped Git publication for adapters and catalog.
- `scheduling.py` validates `schedules.toml` and generates scheduling blocks;
  existing Actions workflows still execute harvesting and continuations.
- `progress.py` records current worker counters and individual errors outside
  certified output; `monitor.py` publishes readable Checks API updates during
  execution and provides run/worker/error inspection without waiting for logs.

A source response first passes native validation, then mapping and shared validation.
Only a validated replacement advances the accepted source record. Full current
listing state remains separate from accepted metadata; transient failures retain
accepted content. Removal requires complete discovery of the relevant scope.
No historical inactive store is created. The current catalog is published as two
compressed archives. Reports replace current reports; completed checkpoints disappear.

## Ownership transition

The [migration matrix](consolidation.md) distinguishes adopted responsibilities
from legacy infrastructure. We adopt source knowledge and contracts, retain the
current grouped/resumable implementation, and supersede older one-repository-per-adapter,
local-only and paired-Dataset-document directions. No other repository is archived,
no unrelated job disabled, and no production database moved by this change.

## One-way data flow

Scheduled or manual harvesting -> validated contributions -> accepted multilingual
collections -> grouping -> portal matching/enrichment -> publication filtering ->
validation -> JSON-LD serialization -> two compressed archives.

The separate weekly portal refresh validates a candidate before replacing the
repository-owned snapshot. The default SCB filter removes only the main PXWeb v1
distributions at publication; accepted descriptions remain available for restoration.
SCB HVD and other providers are unaffected.

Catalog construction is independently callable. It never makes accepted source
files fresher, resumes a checkpoint or dispatches a harvester. Identity/categories
and source evidence are protected by the proposal interface. Current placeholders
produce no changes; future changed proposals must pass common validation.
The [catalog guide](catalog.md) defines current outputs, trust and failure handling.

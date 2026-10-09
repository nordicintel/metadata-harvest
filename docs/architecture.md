# Integration ownership and architecture

`metadata-harvest` owns API integration code, source selection, provider configuration,
metadata semantics, shared Dataset/Provider/Retrieval Result schemas, validation,
independent harvesting jobs, on-demand retrieval and integration documentation.

The catalogue service, website, public HTTP API, production databases, file scrapers
and all BRÅ work remain external. Consumers may use the shared contract for file-backed
observations; this repository adds no file ingestion. Existing PXWeb v1 website
discovery is retained explicitly because replacing it would change coverage.

## Boundaries

- `adapters/pxweb_v1/` and `adapters/pxweb_v2/` own source discovery, requests, native
  validation and retrieval protocol construction; v1 database/location assumptions
  do not leak into v2 discovery.
- `mapping.py`, `validation.py` and `semantics.py` map and validate complete metadata;
  `schemas/` is the authoritative, offline, packaged schema set.
- `retrieval.py` assembles protocol responses by coordinates; only the caller's
  `aiohttp` session performs requests, with caller-owned rate limits and retries.
- `storage.py` owns deterministic grouped serialization and atomic replacements;
  `artifacts.py` owns neutral artifact hashes, publication boundaries, size gates,
  checkpoint paths and outcome classification.
- `workflow.py` retains v1 reconciliation; v2 has its own orchestration, including
  independent codelist retries; `registry.py` explicitly identifies available adapters.
- `runner.py` preserves existing commands and v1 defaults; Actions collectors are
  the only production writers, sharing the `production-harvest` concurrency queue.

A source response first passes native validation, then mapping and shared validation.
Only a validated replacement advances the accepted source record. Full current
listing state remains separate from accepted metadata; transient failures retain
accepted content. Removal requires complete discovery of the relevant scope.
No archive is created. Reports replace current reports; completed checkpoints disappear.

## Ownership transition

The [migration matrix](consolidation.md) distinguishes adopted responsibilities
from legacy infrastructure. We adopt source knowledge and contracts, retain the
current grouped/resumable implementation, and supersede older one-repository-per-adapter,
local-only and paired-Dataset-document directions. No other repository is archived,
no unrelated job disabled, and no production database moved by this change.

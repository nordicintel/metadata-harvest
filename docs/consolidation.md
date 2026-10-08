# API metadata consolidation

## Scope and decisions

This repository owns API metadata adapters, provider configuration, publication,
validation and source mappings. Adapters are importable Python modules with
independently runnable jobs. No Redis, observation warehouse or plugin framework.
All BRÅ work is excluded. New HTML, spreadsheet and PDF ingestion is excluded.
The existing PXWeb v1 web discovery remains a legacy exception: replacing it
with API-only discovery would change coverage and is a separate decision.

Publish **one JSONL per provider and language, separated by adapter**, under
`data/<adapter>/metadata/`. Keep only the latest metadata for datasets still
available at the source. Confirmed source removal deletes metadata and listing
state. There is no inactive archive, reactivation store or snapshot/version model.
Incomplete discovery is an error, not confirmation that missing datasets are gone.

Each adapter owns its current listing state, latest refresh/harvest status and an
unfinished-work checkpoint under its own directory. Reports overwrite the previous
status; completed checkpoints are deleted. Nothing accumulates by run date.

## First change and next steps

1. Preserve the existing output fields and public imports; document their contract.
2. Move v1 discovery and mapping into `adapters/pxweb_v1`, retaining the runner.
3. Compact existing files to JSONL with content verification and stable ordering.
4. Verify retention, incomplete discovery, unchanged runs and limited live retrieval.
5. Implement v2 from the [assessment](pxweb-v2-migration.md), then validate its own job.
6. Migrate SDB, API-only Trafa and metadata-only Kolada before less mature adapters.

Do not disable old schedules until the replacement passes acceptance and has a
successful automated run. Do not archive or delete source repositories.

## Adapter implementation template

- `adapters/<source>/`: discovery, metadata retrieval and source-specific mapping.
  V1's `discover_provider(provider)` yields scopes including completeness,
  observed IDs and errors; its metadata function returns one complete document.
- `storage.py`: deterministic grouped JSONL and atomic file replacement. Use
  `JsonlStore` to merge successful replacements into the last accepted documents.
  Never clear a language directory before fetching replacements.
- `workflow.py`: checkpoints, reconciliation, partial results and reporting.
  Deletion requires complete discovery for every known location. A provider
  failure cannot become an empty successful discovery result.
- `runner.py`: the existing Actions entry point remains compatible. Its pre-existing
  argparse interface is retained, but new functionality belongs in importable
  functions; do not expand this into a CLI framework.
- `providers.json`: the existing v1 configuration stays unchanged. Add a separate
  source configuration only when implementing the next adapter.
- `.github/workflows/`: independent schedules/manual jobs; collectors alone publish.
  Commit validated output, state and report together. Workers checkpoint grouped
  artifacts; failed datasets retain prior successful metadata.

Keep request clients source-specific until the second adapter demonstrates a
shared need. Catalogue and documentation records have separate completeness
contracts from statistical table dimensions/categories.

## Inventory (8 October 2026)

Rows beyond v1/v2 are handoff inventory, not renewed live acceptance claims.

| Source | Implementation / coverage | Automation and gaps | Migration status |
|---|---|---|---|
| PXWeb v1 | This repository; discovery and full category metadata; 19 providers, 47 configured scopes | Daily/manual Actions; latest run incomplete, details below | First adapter extracted; grouped publication implemented |
| PXWeb v2 | `rubenselander/PxWebAPI-scraper`; NordicIntel adapter, harvest and backend implementations | Personal scraper has no workflows; compare pagination, codelists and failure semantics | Assessed; next adapter |
| SDB | `nordicintel/sdb-adapter`; substantial metadata implementation | Replace directory clearing; add independent publication | Next established-source batch |
| Trafa | `nordicintel/trafa-adapter`; substantial implementation | Resolve API/HTML discovery boundary; replace clearing | After SDB |
| Kolada | `rubenselander/kolada-data-scraper`; v3 metadata, groups, municipalities and units | Existing exports depend on observations; establish availability/gender metadata and upstream timestamps | Metadata-only execution path required |
| RowStore / EntryScape tables | `nordicintel/rowstore-adapter` scaffold; `rubenselander/swedish-data-api` transport | Consumer's 20,000-row cap cannot prove full category coverage | Reuse transport; mapping pending |
| UKÄ | `nordicintel/uka-adapter`; scaffold and reference wrapper | Reconcile discovery with `/api/totals/{id}` | Pending |
| Riksbanken, Försäkringskassan, KB | `rubenselander/swedish-data-api` agency clients | Assess completeness separately | Backlog |
| SIRIS | API documentation component in `rubenselander/scb-publiceringskalender` | Scheduled documentation success unverified; isolate from HTML/files | Documentation track |
| Catalogue connectors | `swedish-data-api`, `nordicintel/dataportalen`, `nordicintel/nordicintel-leads` | EntryScape, CKAN, Huwise, ArcGIS Hub, DCAT, researchdata.se OAI-PMH; not complete statistical table metadata | Separate catalogue track |
| DST / Eurostat and further PXWeb providers | Archived implementations / provider candidates | Coverage and current endpoints unverified | Later backlog |

## Verified operational baseline

Base commit: `6bb4a9e`. [Scheduled run 37753836760](https://github.com/nordicintel/metadata-harvest/actions/runs/37753836760)
completed with errors on 8 October. Its committed report
`data/pxweb_v1/reports/harvest.json` (latest status, overwritten by the next run) counts 6,229 reused,
17 updated, 24 failed, 590 deferred and zero unfinished. Errors include 22 MCF
ContentsCode mismatches, two Energimyndigheten HTTP 400 responses and one
Jordbruksverket navigation failure. These remain source investigations, not
diagnosed fixes in this structural change.

The collector published successful data and then explicitly failed the workflow
because results were incomplete. State survives through Git commits on `main`;
Actions artifacts are temporary transport, not the authoritative store.
Production workflows explicitly check out `main`: local tests do not demonstrate
that this branch has run successfully in production.

## Acceptance gate for each migrated adapter

Require selected-provider importable execution, required languages and dimensions,
validated output, stable unchanged metadata, distinct source/harvest timestamps,
retention on failures, protected incomplete discovery, persisted state and a
successful scheduled/manual run. Fixture tests and bounded live checks are
separate evidence; neither a limited smoke test nor a green runner with failed
dataset counts is full acceptance.

## First-change verification

Local conversion reduced 13,694 per-dataset JSON files to 54 JSONL collections:
27 metadata files containing 6,834 documents and 27 listing-state files
containing 6,860 records. Canonical content
hashes before/after conversion were identical (UTF-8, sorted object keys and task
IDs, compact JSON; each task ID precedes its document in the hash input):

| Collection | SHA-256 |
|---|---|
| Listings | `03681f0f92df5025427e46ac5fbf6925ad4d709d29383845fcd75676d796133a` |
| Active metadata | `36579d66df3918af3609276f89903c85b22441cf2b9f3df5866a1a60a1cc92ab` |

All 131 deterministic tests passed, including local HTTP/browser fixtures,
legacy checkpoint/artifact migration, confirmed-removal deletion, failed-refresh
retention, stable repeated JSONL and failure isolation. Ruff lint and format
checks passed. A bounded live check for Medlingsinstitutet's Swedish
`Loner_efter_bransch_arsdata.px` passed category and publication validation with
`value: []`. This is one-table live evidence, not a full provider run.

Independent code review found a mutable legacy-input snapshot bug; a failing
regression test reproduced it and an independent snapshot fixed it. A separate
regression also proved that an unserializable worker result cannot prevent later
valid tables from being saved. These local checks do not establish a successful
scheduled run of the migrated implementation. Scheduled execution uses `main`;
its automated acceptance must be assessed separately from the local verification.

# Repository architecture

Only `retrieval/` is installed by the `metadata-harvest` distribution. The `apps/`
modules run from this checkout using one root `pyproject.toml` and `uv.lock`.

## Ownership

| Location | Responsibility |
| --- | --- |
| `apps/model/` | Extensible StatDCAT Pydantic models, JSON-LD vocabulary, pure normalization and composition, repository validation |
| `apps/configuration.py` | Load authoritative configuration, validate supported adapters and resolve execution settings |
| `apps/harvest/` | Metadata collection, accepted contributions, worker persistence and source identity resolution |
| `apps/harvest/metadata_adapters/` | Twelve adapters with their native clients, parsing, discovery, coverage probes, retry and pacing policy |
| `apps/catalog/` | Offline grouping, matching, enrichment, filtering, quality findings, fingerprints and candidate construction |
| `apps/portal/` | Import, refresh and validate the accepted oppnadata snapshot |
| `apps/publication/` | Streaming archives, integrity and size checks, explicit promotion and scoped Git publication |
| `apps/migrations/` | Historical profile readers, explicit converters, imports and preservation audits |
| `apps/operations/` | CLI routing, scheduling, monitoring, progress and outcome reporting |
| `retrieval/data_adapters/` | Independent consumer observation retrieval implementations |
| `config/` | Authoritative source inventories, profile declaration, operational settings and schedules |
| `schemas/` | Generated contracts and labelled supporting schema resources |

Source responses pass native parsing and validation before accepted records advance.
Failed updates preserve accepted contributions; complete discovery is required to
establish removal. Checkpoints retain existing task identities and collection names.
The worker representation is still used internally; its conversion lives in harvesting.
Historical profile readers do not execute migration commands.

## Dependencies and data flow

```text
model + configuration -> harvest -> accepted contributions
portal -> accepted portal snapshot
accepted contributions + accepted snapshot -> catalog candidate
catalog candidate -> explicit publication -> production artifacts
operations -> application commands
retrieval -> aiohttp + standard library + its pure helpers
```

Model composition is pure and shared by accepted persistence and catalog assembly.
Model validators never load source inventories or perform network/persistence work.
Catalog construction never starts a harvest, refreshes a portal snapshot or promotes
production output. Archive utilities may write temporary candidate streams without
publishing them. Configuration paths resolve from the checkout, independently of
where candidate data is stored.

Consumer retrieval owns protocol requests and observation responses. Harvesting
owns metadata inference and coverage probes even when they fetch observations.
Harvesting can use identical pure helpers from retrieval, but cannot invoke its
consumer functions. The optional distribution dispatcher never imports repository
applications, Pydantic or compression dependencies.

## Commands and resources

Run `uv sync --locked --extra harvest`, then use `uv run python -m
apps.operations.harvest --help` for harvesting commands. The other operation modules
are `catalog`, `portal`, `scheduling`, `monitor` and `outcome`. Scripts delegate
migration/import work to application functions. `scripts/migrations/` contains those
entry points; `scripts/generate/` owns contract and documentation generators,
`scripts/checks/` contains offline and CI verification tools, and `scripts/live/`
contains opt-in provider checks and their report summaries. Run scripts from the
checkout with `uv run python scripts/<purpose>/<tool>.py`. No old application import paths are supported.

New catalog output uses two deterministic Zstandard level 6 streams with checksums:
`catalog.jsonl.zst` and `codelists.jsonl.zst`.
Production promotion validates source/dependency fingerprints and candidate hashes
before mutation. Moving implementation paths invalidates old candidates; it does
not regenerate accepted data or production archives.

Generated contracts and reference documentation are checked by the two generator
scripts. [Configuration](configuration/README.md), [harvesting](harvest/README.md),
[catalog](catalog/README.md), and
[retrieval](retrieval/README.md) document the corresponding interfaces.

## Workflows

`checks.yml` verifies development changes. `live-pxweb.yml` and
`live-metadata.yml` run explicit provider matrices and summarize retained evidence;
each job installs its own environment. The `harvest*.yml` workflows collect accepted
metadata, `refresh-portal.yml` refreshes the accepted portal snapshot, and
`catalog.yml` builds and publishes the shared catalog. Scheduled blocks are generated
from `config/schedules.toml`; workflow filenames remain execution identities in the
adapter registry. Publication jobs share the `production-publication` lock.

## Command routing

Run `uv run python -m apps.operations.<module> --help` from the checkout.

| Module | Role |
| --- | --- |
| `harvest` | Adapter workers, collection and scoped publication |
| `catalog` | Offline candidate build and explicit production promotion |
| `portal` | Accepted snapshot import, refresh and publication |
| `scheduling` | Resolve schedules and synchronize workflow blocks |
| `monitor` | Live progress and individual error inspection |
| `outcome` | Report failure status for the selected scope |

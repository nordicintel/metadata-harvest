# Planned repository structure

Status: implemented and verified on 2026-10-10. See [cutover notes](migrations/repository-structure.md) for ownership decisions and verification.

## Context and intent

The repository contains metadata harvesting, StatDCAT modelling, catalog construction, portal enrichment and production publication. Only the consumer observation retrieval library is intended to be installable. Everything else runs from the repository.

The previous extraction moved consumer retrieval into `retrieval/` and the repository application into `harvesting/`, but left unrelated responsibilities mixed directly under `harvesting/`. Compatibility forwarders and harvesting retrieval exports were subsequently removed. The next change should organise the actual responsibilities, not merely rename that directory again.

Use one root `pyproject.toml` and one `uv.lock`. The `apps/` modules are repository code, not independently packaged projects. Do not introduce nested projects, workspaces, forwarding modules or transitional duplicate trees.

## Target layout

```text
apps/
  __init__.py
  configuration.py             # load and resolve authoritative configuration
  model/                       # authoritative StatDCAT Pydantic models and JSON-LD
  harvest/                     # metadata discovery, collection and reconciliation
    metadata_adapters/         # adapter-specific parsing, clients and probes
    accepted.py                # shared accepted contribution persistence
  catalog/                     # grouping, matching, enrichment and candidates
  portal/                      # snapshot import, refresh and validation
  publication/                 # archive I/O, verification and production promotion
  migrations/                  # explicit legacy readers, converters and ledgers
  operations/                  # scheduling, progress, monitoring and CLI routing

config/
  adapters.json
  organisations.json
  catalogs.json
  catalog.toml
  catalog-matches.json
  schedules.toml
  profile.json                 # NordicIntel profile version and upstream pin

schemas/                       # generated contracts and supporting schema resources
data/                          # accepted collections and production artifacts
docs/                          # organised by application responsibility
retrieval/                     # the only installable Python library
  data_adapters/               # rename existing adapters/; use Python-safe underscores
  ...                          # optional dispatcher and necessary pure runtime helpers
scripts/                       # thin entry scripts and maintenance utilities
tests/                         # organised by application responsibility
  ...                          # separate integration and explicit live checks

pyproject.toml
uv.lock
README.md
REPOSITORY-STRUCTURE-PLAN.md
```

Use ordinary Python package initialisers where needed. The tree shows responsibility boundaries, not a requirement to create empty folders or unnecessary layers. Repository infrastructure such as `.github/` remains in place.

No JSON configuration files remain at the repository root. The only root TOML file is `pyproject.toml`; `uv.lock` remains at root.

## Ownership and behaviour

### Model and schemas

`apps/model/` owns the extensible NordicIntel StatDCAT profile: Pydantic models, JSON-LD serialisation and vocabulary definitions. Use standard vocabulary properties where their meaning fits; retain meaningful custom and unfamiliar source attributes without redundant copies of successfully mapped fields.

Root `schemas/` contains generated contracts, not a competing handwritten definition of the Pydantic model. Preserve necessary imported schema resources and their provenance; distinguish these from generated outputs. Generators must write to the new paths and checks must detect stale generated files.

### Configuration

The files in `config/` are authoritative. `apps/configuration.py` loads and validates them together and resolves organisation, adapter, service and catalog references. Remove independently packaged copies of configuration: the retrieval distribution does not need source inventories.

Preserve explicit empty values, zeroes and `False` when applying defaults. Publisher remains a resource-specific role; organisations, adapters, concrete services and catalogs remain distinct concepts.

### Harvesting and retrieval

`apps/harvest/` owns discovery, metadata collection, reconciliation, checkpoints and accepted contribution persistence. Each metadata adapter owns its clients, retries, pacing, coverage discovery, metadata inference and endpoint probes—even when a probe reads observations from a data endpoint.

`retrieval/data_adapters/` owns the twelve consumer observation retrieval implementations. Each adapter retains its protocol-specific requests, response interpretation and errors. There is no mandatory common retrieval engine. `retrieve_distribution()` remains optional consumer dispatch; harvesting does not use it.

Retrieval must never import `apps/`, configuration inventories, full StatDCAT models, portal tools, migration tools or publication code. Its runtime dependencies remain `aiohttp` and the standard library. Harvesting may import shared pure helpers from retrieval only where the semantics genuinely agree.

Use underscores in `metadata_adapters` and `data_adapters` so normal Python imports work. Update retrieval entry-point targets when renaming the directory.

### Portal and catalog

`apps/portal/` imports, refreshes and validates the repository-owned portal snapshot. It accepts a candidate only after validating completeness and scope; failed refreshes preserve the previous accepted snapshot.

`apps/catalog/` consumes accepted contributions and the accepted portal snapshot offline. It owns grouping, matching, enrichment, publication filtering and candidate construction. Matching must not exclude unmatched harvested datasets or turn ambiguous matches into accidental merges.

Catalog construction must not start a harvest, refresh the portal snapshot or implicitly promote production output.

### Publication

`apps/publication/` owns archive reading/writing, integrity and size verification, and explicit production promotion. It consumes accepted candidate output. Preserve the existing production format, compression decisions, SCB publication switch and scoped publication behaviour.

Production publication is an explicit operation, not a side effect of development, testing, harvesting or candidate construction.

### Migrations and operations

`apps/migrations/` contains explicit legacy readers/converters and migration ledger tooling. These are deliberate recovery and conversion tools, not aliases that keep superseded application import paths alive.

`apps/operations/` owns scheduling, progress, monitoring and CLI routing. Adapter-specific harvesting decisions and retry policy stay in harvesting. Operations must not become a catch-all for unrelated business logic.

Scripts should delegate to application functions rather than duplicate their implementation. Documentation and tests should follow responsibility boundaries; integration tests cover cross-application behaviour, and live checks remain explicitly separate from offline fixtures.

## Dependency and data flow

```text
configuration + model -> harvest -> accepted contributions
portal -> accepted portal snapshot
accepted contributions + portal snapshot -> catalog candidate
catalog candidate -> publication -> production artifacts
operations -> invokes the applications

retrieval -> aiohttp + standard library + its own pure helpers
```

## Implementation constraints and acceptance

- Inventory current modules and their consumers before assigning destination paths; move implementations rather than copying them.
- Update imports, resource loading, CLI commands, workflows, generators, configuration paths, entry points, docs and tests together.
- Delete superseded paths and exports. Do not add forwarding modules, compatibility shims or a transition release for this reorganisation.
- Preserve dataset/distribution identifiers, source identities, task keys, metadata attributes, retrieval behaviour, accepted contributions and failure recovery.
- Preserve the root installation contract: the distribution is `metadata-harvest`, its Python namespace is `retrieval`, and the wheel includes only retrieval code.
- Verify adapter request/result equivalence, harvesting independence from consumer retrieval, configuration resolution, resource paths, generated contracts, repository commands and isolated wheel installation.
- Reuse completed verification where code and behaviour are unchanged; do not repeat expensive corpus checks merely because files moved.
- No provider reharvest, metadata conversion, production publication, production archive recompression or full-corpus reuse check is implied by this structural change.

## Original planning checkpoint

`main` contains the retrieval extraction at `2769bded` and the compatibility cleanup at `e2e66d35`. That checkpoint preceded the now-implemented `apps/` and `config/` cutover.

An ignored, obsolete `src/` tree containing Python bytecode remained after the source move because automatic approval review blocked cache deletion. It is not authoritative source and must not be mistaken for an application to migrate. An older detached integration worktree also remained outside the main checkout; its branch history was preserved in archive tags. Treat physical workspace cleanup separately from moving tracked implementation files.

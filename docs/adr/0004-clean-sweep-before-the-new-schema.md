# ADR 0004: Clean sweep before the new schema

Status: accepted (2026-10-16)

## In one paragraph

Before the new schema (ADR 0002) is designed, everything that exists only to produce,
read, convert, upgrade or publish StatDCAT is deleted, and the few model-agnostic
pieces hiding inside those modules are moved to a new top-level `schema/` folder as
seeds. What remains is: the harvest side of every adapter, the source store, the
registry (ADR 0003), the parse loop, the retrieval library's data adapters, the
portal snapshot, and operations. Nothing under `data/` is deleted until every adapter
has harvested into `source/`.

## Two facts this plan rests on

1. **No source files exist yet.** The harvest-to-source cutover (2026-10-16) landed in
   code, but no adapter has run since. `data/<adapter>/source/` and `manifest.json`
   do not exist anywhere. The StatDCAT collections under `data/<adapter>/metadata/`
   are the only harvested metadata in the repository.
2. **Adapters do not emit StatDCAT.** Each adapter's `mapping.py` and `parse.py`
   produce one JSON-stat2 document per language with an `extension.nordicintel`
   block, validated by `schemas/legacy-dataset-metadata.schema.json`. Only
   `apps/parsing/contributions.py` turns that into StatDCAT. That worker document is
   already close to the tabular model ADR 0002 wants, so the mapping side of every
   adapter is kept and the schema session edits it.

## Words used in this document

- **Worker document**: the JSON-stat2 per-language document an adapter's mapping
  produces from stored source responses. Seed of the new tabular model.
- **Accepted collection**: today, the StatDCAT JSON-LD files under
  `data/<adapter>/metadata/`. After the schema session, files in the new model.
- **Seed**: a small, tested, model-agnostic piece of code or schema kept in
  `schema/` to start the new model from. Not a finished design.
- **Delete**: remove from the working tree. Git history is the only record kept.

## The decision

### What is deleted

Code:

| Path | Why |
| --- | --- |
| `apps/model/models.py`, `apps/model/vocabulary.py` | StatDCAT Pydantic models and JSON-LD context |
| `apps/parsing/contributions.py` | worker document to StatDCAT and back; SKOS codelists |
| `apps/parsing/accepted.py`: `adapter_for`, `contribution_documents`, `load_contributions`, `model_contributions`, `as_contributions` | legacy and StatDCAT-only |
| `apps/catalog/` entirely | assembles and enriches StatDCAT graphs; the build skeleton is in git if a future catalog wants it |
| `apps/publication/archives.py`, `apps/publication/candidates.py` | only the catalog used them |
| `apps/migrations/`, `scripts/migrations/` | profile upgrades and conversions (ADR 0003) |
| `apps/harvest/sources.py`, `apps/configuration.py`, `apps/model/configuration.py` | replaced by `registry/` (ADR 0003) |
| `apps/operations/catalog.py`; the catalog branch of `apps/operations/outcome.py` | catalog CLI |
| `apps/harvest/storage.py`: `PROFILE_VERSION` stamping, `migrate_storage`, `validate_dataset` | StatDCAT coupling in every harvest; dead code |
| the `fetch_metadata` and `fetch_codelist` wrappers in all 12 adapter `__init__.py` | one caller, a StatDCAT test |
| `retrieval/distribution.py`: the JSON-LD projection (`_operational_metadata` and the `ni:`/`qb:`/`skos:` readers) | the only StatDCAT reader in the installable library; dispatch, `ADAPTERS` and selection validation stay |
| `scripts/checks/check_statdcat_upstream.py`, `scripts/live/check_statdcat_live.py`, `scripts/generate/generate_schema_reference.py` | StatDCAT checks and generators |
| dev dependencies `pyshacl`, `rdflib` | only the upstream check used them |

Schemas, configuration, workflows:

| Path | Why |
| --- | --- |
| `schemas/dataset-metadata.schema.json`, `harvest-contribution.schema.json`, `codelist.schema.json`, `statdcatap-3.0.0-diagnostic.ttl` | generated from or about StatDCAT |
| `config/profile.json`, `config/catalog-matches.json`, `[catalog]` in `config/settings.toml` | StatDCAT pin, empty overrides, catalog setting |
| `.github/workflows/catalog.yml`; the `generate_schema_reference --check` step in `checks.yml` | catalog only |

Tests and docs:

| Path | Why |
| --- | --- |
| `tests/catalog/`, `tests/configuration/` and `tests/fixtures/configuration-legacy.json`, `tests/migrations/`, `tests/model/test_statdcat.py`, `tests/parsing/test_accepted.py`, `tests/publication/test_candidates.py` | StatDCAT, catalog or legacy configuration only. The `legacy()` worker-document fixture in `test_statdcat.py` moves to `tests/support/` first; five files import it. |
| the matching and enrichment tests in `tests/portal/test_portal.py`; archive and candidate tests in `tests/publication/test_catalog.py`; the catalog branch in `tests/operations/test_outcome.py`; `apps.migrations` uses in `tests/integration/` | mixed files, StatDCAT parts removed |
| `docs/catalog/`, `docs/model/`, `docs/migrations/` (98 MB), `docs/publication/compression-trial.json`, `docs/reference/StatDCAT-AP 3.0.0.html` and its provenance file, `docs/superpowers/plans/` | StatDCAT docs, audit ledgers, executed plans |

Data, **only at step 5 below**:

| Path | Size | Why |
| --- | --- | --- |
| `data/<adapter>/metadata/` | 221 MB | StatDCAT accepted collections; regenerated by the new parse |
| `data/catalog/` | 145 MB | StatDCAT production archive |

### What moves

| From | To | Why |
| --- | --- | --- |
| `apps/model/composition.py` merge algorithms (`_merge_text`, `_union`, `merge_codelists`, `_retain_conflict`) | `schema/merge.py` | per-language documents into one multilingual record is still needed |
| `apps/model/normalization.py` period and frequency helpers | `schema/periods.py` | pure; `stable_id` is replaced by `registry.mint` |
| `apps/model/validation.py` schema loader and `validate_retrieval` | `schema/validation.py` | generic JSON-schema validation used by harvest, live checks and tests |
| `schemas/legacy-dataset-metadata.schema.json` | `schema/tabular.schema.json` | the worker contract is the seed of the tabular model |
| `schemas/kolada_municipalities.json` | `apps/harvest/metadata_adapters/kolada/` | adapter data, not a schema |
| `JsonlStore`, `refresh_identity` from `apps/harvest/storage.py` | `apps/harvest/metadata_adapters/pxweb_v1/` | pxweb_v1 only; pxweb_v2 imports `JsonlStore` from there |
| `apps/model/configuration.py`, `apps/configuration.py` | `registry/` | ADR 0003 |
| `Dataset.from_jsonld` reader | nowhere; deleted with the models | accepted collections are regenerated |

The remaining `schemas/` files (`provider.schema.json`, `retrieval-result.schema.json`,
`source-configuration.schema.json`) move into `schema/` too, so there is one folder.
`source-configuration.schema.json` is regenerated from `registry/models.py`.

### What is kept, with edits

- `apps/parsing/build.py`: the build and apply loop. Three imports change: the
  registry for bindings, `schema.validation` for the worker check, and a plain
  writer in `accepted.py` (sqlite staging, deterministic gzip, per-owner
  collections, round-trip digest) typed to dicts, not `Dataset`.
- `apps/harvest/validation.py`: worker-document validation stays; `Dataset`
  branches go.
- `apps/operations/harvest.py`, `parsing.py`, `outcome.py`: registry instead of
  `apps.configuration`; `legacy_configuration` and catalog branches go.
- `entryscape/discovery.py`: `SourceIdentity` and catalog scopes become registry
  calls.
- `scripts/checks/check_distribution.py`: its fixture becomes a native record.
- The 11 `test_parse` goldens under `tests/fixtures/<adapter>/accepted/`: regenerated
  by the schema session; until then those tests compare worker documents.
- `README.md`, `AGENTS.md`, `docs/architecture.md`, `docs/configuration/README.md`,
  `docs/parsing/README.md`, `docs/retrieval/README.md`: StatDCAT wording and the
  ownership table.

### What is kept untouched

- Every adapter's harvest side: `client`, `api`, `discovery`, `workflow`, `state`,
  `probe`, `source` response parsers (about 16,700 lines). ADR 0001 shrinks the
  workflows later.
- Every adapter's mapping side (about 6,100 lines). The schema session edits it.
- pxweb_v1, including its 29 bindings. It already stores per-table source files.
- `apps/harvest/source_store.py`, `currency.py`, `artifacts.py`, `logs.py`,
  `registry.py`.
- `apps/portal/snapshot.py`, `apps/operations/portal.py`, `refresh-portal.yml`,
  `[snapshots.oppnadata]`: Tillväxtverket's parse reads the oppnadata snapshot.
- `apps/publication/artifacts.py`, `git.py`.
- `apps/operations/monitor.py`, `progress.py`, `scheduling.py`.
- `retrieval/` except the projection in `distribution.py`.
- All `harvest*.yml`, `live-*.yml`, `slow-tests.yml`, `checks.yml`.
- `config/adapters.json`, `organisations.json`, `catalogs.json`,
  `schedules.toml`, `settings.toml` minus `[catalog]`.
- 54 harvest, retrieval and operations test files and `tests/support/`.

## Order of work

1. **Registry** (ADR 0003 steps 1 to 3).
2. **Cut the StatDCAT layer**: the deletion and move tables above, in one branch,
   with `uv run pytest` green at the end. The `PROFILE_VERSION` stamping in
   `storage.py` goes first because every harvest imports it.
3. **Harvest lifecycle** (ADR 0001).
4. **Schema session**: design the tabular model from `schema/tabular.schema.json`;
   rewrite each `parse.py`.
5. **Regenerate and delete old data**: with `data/<adapter>/source/` present for
   every adapter, run the new parse, regenerate accepted collections and goldens,
   then delete `data/<adapter>/metadata/` and `data/catalog/`.

Harvesting into `source/` is not a step of the sweep. The scheduled harvest workflows
fill `source/` on their own cadence (the first dispatch of all twelve was 2026-10-16);
the only thing that waits on them is step 5. Nothing under `data/` is deleted before
every adapter has a `source/` directory and manifest.

## Consequences

- About 9,700 lines of code, schemas and tests leave, plus 14,500 lines of spec
  HTML and about 460 MB of data and audit ledgers once step 5 completes.
- About 2,350 lines are deferred to the schema session rather than deleted, all of
  it adapter mapping or seeds in `schema/`.
- Between step 3 and step 5 the repository has no accepted-collection format: parsing
  can build worker documents and validate them, but nothing publishes a catalog. That
  gap is deliberate.
- Consumers of the published `catalog.jsonl.zst` lose it at step 5. There are none
  in production.

## Amendments to earlier ADRs

- ADR 0003 said the audit reports under `docs/migrations/` stay as history. They are
  deleted; git history is the record.
- ADR 0001's glossary says parsing produces StatDCAT. Parsing produces the model
  from ADR 0002; until that exists it produces worker documents.

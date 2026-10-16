# Clean sweep

This document is the single statement of where the repository is going and what is
removed to get there. It replaces every earlier design note. Written 2026-10-16.

## 1. Where we are going

The repository harvests statistics metadata from Swedish providers. Today it converts
that metadata into StatDCAT, an RDF profile squeezed into JSON-LD, and publishes a
StatDCAT catalog. That ends.

The new target is one self-defined JSON schema of our own:

- Similar to DCAT-AP in vocabulary, but plain JSON, no JSON-LD, no RDF.
- Multilingual text is a first-class type, not language-tagged strings.
- Typed subtypes so each kind of source is described by the fields it actually has.
- A common tabular model close to how PXWeb v2 describes a table: the listing entry,
  the JSON-stat2-style metadata (dimensions, categories, units, time) and the codelists,
  all multilingual, with normalisation of dimensions and categories built in. Nearly
  every adapter fits this.
- A more flexible tabular form for the sources that do not fit, such as some
  Trafikanalys entries and the Dataanalys entries not supported today. SDMX is the
  candidate to evaluate.
- Organisations will later be defined in and matched against the
  `nordicintel/organisations` repository, which owns the organisation model.
  Identifiers for datasets will be built from type, creator organisation and a
  normalised dataset code. Nothing about identifiers is decided yet.

The schema is designed in its own session after the sweep. The sweep's job is to leave
a repository small enough that the schema can be built without working around anything.

## 2. Principles

1. **This is not production.** Nothing is kept for compatibility, history or safety.
   Git is the record. If a thing has no caller after the sweep, it is deleted, not
   moved.
2. **Fewer files, fewer modules, fewer concepts.** When two designs are equal, the one
   with less code wins.
3. **Harvest and parse are separate.** Harvesting stores provider responses as files,
   exactly as received. Parsing reads those files later, offline, and produces the
   model. Nothing else touches either side.
4. **One shape for every adapter.** A new adapter is written by copying the shape, not
   by copying another adapter.
5. **Configuration is a parameter.** Loaded once, validated once, passed in. No ambient
   globals, no context variables.
6. **New top-level code lives outside `apps/`.**

## 3. Words

- **Adapter**: one provider integration, one folder under
  `apps/harvest/metadata_adapters/`.
- **Source files**: the raw provider responses an adapter fetched, stored under
  `data/<adapter>/source/` and indexed by `manifest.json`. This is the only thing
  harvesting produces.
- **Worker document**: what an adapter's mapping code produces today from source files:
  one JSON-stat2 document per language with an `extension.nordicintel` block. It is
  validated by `schemas/legacy-dataset-metadata.schema.json`. It is close to the
  tabular model above and is the starting point for the schema work. It is **not**
  StatDCAT.
- **Lifecycle**: the loop around a harvest run: copy previous files, discover what the
  provider lists, fetch each item, retry once, decide what is published or withheld,
  write state and reports, apply the result. Today it is copied into twelve
  `workflow.py` files. After the sweep it exists once.
- **Published / withheld**: a stored item is published when its endpoint answered in
  this run and it was verified recently; otherwise withheld, files kept, skipped by
  parsing. Rule lives in `apps/harvest/currency.py`.
- **Key**: identity of one stored item, `<language>/<code>`.

## 4. How the sweep is done

- On a branch. The first commit deletes the contents of `data/` so no old artifact or
  design survives. Harvest workflows recreate source files after the branch lands.
- Nothing that runs longer than a few minutes is started without stating its duration
  and getting a go.
- Tests and lint are green at every commit: `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`.
- The finished branch contains none of these strings outside this document:
  `statdcat`, `StatDCAT`, `jsonld`, `json-ld`, `entrysoft`, `PROFILE_VERSION`,
  `profile_version`, `migrations`.

## 5. What goes

Code:

- `apps/model/models.py`, `apps/model/vocabulary.py`: the StatDCAT models and JSON-LD
  context.
- `apps/parsing/contributions.py`: worker document to StatDCAT and back, SKOS codelists.
- In `apps/parsing/accepted.py`: `adapter_for`, `contribution_documents`,
  `load_contributions`, `model_contributions`, `as_contributions`.
- `apps/catalog/` entirely, with `apps/publication/archives.py`,
  `apps/publication/candidates.py`, `apps/operations/catalog.py`, the catalog branch
  of `apps/operations/outcome.py`, `.github/workflows/catalog.yml`, `docs/catalog/`,
  the `[catalog]` table in `config/settings.toml`.
- `apps/migrations/`, `scripts/migrations/`, `docs/migrations/`.
- `apps/harvest/sources.py`, `apps/configuration.py`, `apps/model/configuration.py`
  (replaced by `registry/`, section 7).
- In `apps/harvest/storage.py`: the `PROFILE_VERSION` stamp written into every
  `result.json` (lines 19 and 81-95; every harvest imports it, so cut this first),
  `migrate_storage`, `validate_dataset`.
- The `fetch_metadata` and `fetch_codelist` wrappers in all twelve adapter
  `__init__.py` files. Their only caller is `tests/model/test_statdcat.py`.
- In `retrieval/distribution.py`: the JSON-LD reader (`_operational_metadata` and the
  `ni:`, `qb:`, `skos:` lookups). The `ADAPTERS` set, selection validation and
  dispatch stay.
- `scripts/checks/check_statdcat_upstream.py`, `scripts/live/check_statdcat_live.py`,
  `scripts/generate/generate_schema_reference.py`.
- Dev dependencies `pyshacl` and `rdflib`.

Schemas, configuration, workflows:

- `schemas/dataset-metadata.schema.json`, `schemas/harvest-contribution.schema.json`,
  `schemas/codelist.schema.json`, `schemas/statdcatap-3.0.0-diagnostic.ttl`.
- `config/profile.json`, `config/catalog-matches.json`.
- The `generate_schema_reference --check` step in `.github/workflows/checks.yml`.
- The catalog `scopes` field in `config/catalogs.json` and its model. The riksbank URL
  regex that split catalogs by database. Both served the StatDCAT catalog only.

Tests and docs:

- `tests/catalog/`, `tests/configuration/`, `tests/fixtures/configuration-legacy.json`,
  `tests/migrations/`, `tests/model/test_statdcat.py`, `tests/parsing/test_accepted.py`,
  `tests/publication/test_candidates.py`. The `legacy()` worker-document fixture in
  `test_statdcat.py` is imported by `tests/parsing/test_build.py` and four other files;
  move it to `tests/support/` first.
- Matching and enrichment tests in `tests/portal/test_portal.py`; archive and candidate
  tests in `tests/publication/test_catalog.py`; the catalog branch in
  `tests/operations/test_outcome.py`; `apps.migrations` uses in `tests/integration/`.
- `docs/model/`, `docs/publication/compression-trial.json`,
  `docs/reference/StatDCAT-AP 3.0.0.html` and its provenance file,
  `docs/superpowers/plans/`.
- Every occurrence of `entrysoft`.

Data: everything under `data/`, in the first commit of the branch.

## 6. What stays

- Every adapter's harvest code: clients, discovery, fetch, probes, response parsers.
  About 16,700 lines across twelve adapters.
- Every adapter's mapping code that produces worker documents. About 6,100 lines. The
  schema session rewrites its output format; the sweep does not touch it.
- pxweb_v1 with its 29 providers. pxweb_v2 does not cover them.
- `apps/harvest/source_store.py`, `currency.py`, `artifacts.py`, `logs.py`,
  `registry.py`.
- `apps/parsing/build.py`, the build and apply loop, and the generic parts of
  `apps/parsing/accepted.py`: reading records, collection naming, staging, deterministic
  gzip, digest check. Retyped to plain dicts.
- `apps/portal/snapshot.py`, `apps/operations/portal.py`, `refresh-portal.yml`.
  Tillväxtverket's parse reads the data.europa.eu snapshot for titles, descriptions,
  contacts and licence.
- `apps/publication/artifacts.py` and `git.py`.
- `apps/operations/harvest.py`, `parsing.py`, `outcome.py`, `monitor.py`,
  `progress.py`, `scheduling.py`.
- `retrieval/` except the JSON-LD reader named above.
- `config/adapters.json`, `organisations.json`, `catalogs.json`, `schedules.toml`,
  `settings.toml` minus `[catalog]`.
- All `harvest*.yml`, `live-*.yml`, `slow-tests.yml`, `checks.yml`.
- `scripts/live/` provider checks, `scripts/checks/check_live_progress.py`,
  `scripts/checks/check_distribution.py` (fixture rewritten to a native record),
  `scripts/generate/generate_configuration.py` (regenerates from `registry/`).
- Fifty-four harvest, retrieval and operations test files and `tests/support/`.

Kept as seeds for the schema work, in a new top-level `schema/` folder:

- `schemas/legacy-dataset-metadata.schema.json` renamed `schema/tabular.schema.json`.
- The merge algorithms from `apps/model/composition.py` (per-language documents into
  one multilingual record).
- The period and frequency helpers from `apps/model/normalization.py`.
- The JSON-schema loader and `validate_retrieval` from `apps/model/validation.py`.
- The remaining `schemas/*.json`: `provider.schema.json`,
  `retrieval-result.schema.json`, `source-configuration.schema.json`.
- `schemas/kolada_municipalities.json` moves into the kolada adapter folder instead; it
  is adapter data.

## 7. Registry

A new top-level `registry/` replaces `apps/configuration.py`,
`apps/model/configuration.py` and `apps/harvest/sources.py`. It does exactly this:

- Load `config/adapters.json`, `organisations.json`, `catalogs.json` once and validate
  cross-references.
- Return the typed service bindings for an adapter, in configured order. Adapters,
  parsing and the CLI receive a `ServiceBinding`, not a dict.
- Return the owner organisation of a binding.

Nothing else. No URL recognition, no identifier minting, no aliases, no sub-catalogs.
Those are added only when the schema session has a caller for them.

## 8. Harvest lifecycle and adapter folder

One module owns the lifecycle. Each adapter folder contains exactly:

```text
apps/harvest/metadata_adapters/<name>/
├── __init__.py      empty
├── client.py        HTTP calls to the provider: URLs, parameters, decoding
├── harvest.py       ADAPTER: how to discover what exists and fetch one item
├── parse.py         documents() and withheld_keys(): source files -> documents
└── mapping.py       optional: pure functions shared by harvest.py and parse.py
```

Adapters may add private modules for their own use. Nothing outside the folder imports
them, except the live check scripts which may import `client.py` and `ADAPTER`.

`harvest.py` exposes one object with: `name`, `provider_code`, the name of its state
collection, its count names, the CLI selection options it accepts, a `client(binding)`
factory, `discover(run)` yielding scopes with their tasks, `fetch(run, task)` returning
a stored result or raising `Unreachable`, `forget(key)` naming companion files to drop,
and `scope_failures(report)`. The lifecycle owns everything else: directory checks,
baseline copy, the source store, state, retry with cooldown, published or withheld,
reports, `result.json`, and `collect()`.

Kolada and EntryScape do not fit the fetch-and-settle loop (change feed with a request
budget; many providers with a hard stop when a host blocks). They keep their own loops
and use only the lifecycle's disk-handling half. pxweb_v1 is left as it is.

The file layout written by a harvest, `source/`, `manifest.json`,
`state/current.jsonl.gz`, `reports/latest.json`, is a contract with parsing and does
not change.

## 9. Tests after the sweep

- The registry is tested on a small synthetic configuration (a few bindings, two
  owners), plus one smoke test that loads the real `config/`.
- The lifecycle is tested once with a toy adapter. Each real adapter keeps a discover
  test, a fetch test and one end-to-end run that also calls its `parse.documents`.
- Parse goldens under `tests/fixtures/<adapter>/accepted/` are regenerated when the
  schema exists. Until then those tests compare worker documents.
- No test asserts production inventory counts.

## 10. After the sweep

The schema session designs the tabular model from `schema/tabular.schema.json`,
rewrites each adapter's `parse.py` to produce it, and decides identifiers and what a
catalog is. Then the harvest workflows run and parsing regenerates collections from
source files.

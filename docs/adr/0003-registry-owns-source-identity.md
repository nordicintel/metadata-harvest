# ADR 0003: One registry owns source identity

Status: accepted with retractions (2026-10-16)

> **Retracted the same day, before implementation.** The following parts added
> complexity for callers that ADR 0004 deletes, and are withdrawn: splitting
> riksbank into two service bindings and two catalogs; giving the two EntryScape
> tenants their own bindings; `aliases` on organisations and `owner_by_name`;
> `recognise(url)` (its only callers were catalog enrichment, matching and
> filtering, all deleted); and fixing the `mint` rule now (identifiers are
> redesigned with the new schema). What stands: one root-level `registry/` that
> loads and validates the three inventories, returns typed `ServiceBinding`s per
> adapter, and replaces `apps/configuration.py`, `apps/model/configuration.py` and
> `apps/harvest/sources.py`. Catalog `scopes` and the riksbank regex are deleted
> with their callers, not re-expressed. The implementing agent plans the exact
> interface from the callers that remain after the sweep.

## In one paragraph

"Whose dataset is this, which service serves it, which catalog lists it, and what is
its stable ID" is answered today in seven places with slightly different rules. This
ADR creates one module, `registry/`, at the repository root. It loads the three
inventories in `config/` (adapters, organisations, catalogs), exposes a `Registry`
object with three capabilities (lookup, recognise, mint), and is the only code that
reads those files. Everything that duplicated it is deleted, along with every legacy
and compatibility path found on the way.

## Words used in this document

- **Owner**: the organisation responsible for a source. Has a short code such as
  `scb` or `riksbank` and an entry in `organisations.json`.
- **Service binding**: one configured API endpoint for one adapter, with its
  `source_code`, `endpoint_url` list, publisher and options. Lives in `catalogs.json`
  under `services`. Adapters receive a binding as their "provider".
- **Catalog**: a configured listing that groups the datasets of one or more service
  bindings. Also in `catalogs.json`.
- **Recognise**: given a URL found in an external record, say which service binding
  it belongs to.
- **Mint**: build a stable identifier such as `urn:nordicintel:dataset:scb:AM0101`.
- **Legacy**: any code that exists only to read, rename or upgrade something written
  under an earlier rule. This repository is not in production, so legacy is deleted,
  never preserved.

## The problem

The same questions were answered in:

| Place | What it did |
| --- | --- |
| `apps/harvest/sources.py` | inventory view, owner lookup, service by URL with a regex fallback, a riksbank-only database regex, catalog scope matching |
| `apps/catalog/enrichment.py` | hardcoded hosts for four adapters, then an inventory prefix match |
| `apps/catalog/matching.py` | hardcoded `scb` and `rka` publisher aliases, pxweb prefix match |
| `apps/catalog/filtering.py` | three hardcoded SCB hosts |
| `apps/parsing/contributions.py` | owner lookup, plus kolada and entryscape branches when minting IDs |
| `apps/parsing/accepted.py` | owner lookup again, and `adapter_for` guessing an adapter |
| `apps/catalog/assembly.py` | owner lookup a fourth time |

The rules disagreed in small ways: casefold versus lower, port checked or not. The
configuration was reached through an ambient three-call protocol (`use_configuration`,
`activate_configuration`, `load_configuration`) that callers had to invoke in an
unwritten order, and the catalog read configuration through two channels.

## The decision

### `registry/` at the repository root

```text
registry/
├── __init__.py       exports Registry and the model types
├── models.py         AdapterDefinition, ServiceBinding, CatalogConfiguration, Organisation
├── load.py           read config/*.json, validate cross-references
└── registry.py       the Registry object
```

It depends on Pydantic and the standard library. It does not import `apps/`.
`apps/harvest`, `apps/parsing` and `apps/catalog` import from it. It owns the three
inventories only; `settings.toml` and `schedules.toml` stay with operations.

### The interface

```python
registry = Registry.load(config_dir)        # once per process, then passed as a parameter

# lookup
registry.binding(adapter, source_code) -> ServiceBinding          # raises KeyError
registry.binding_by_id(service_id) -> ServiceBinding
registry.bindings(adapter) -> list[ServiceBinding]                # in configured order
registry.owner(binding) -> Organisation
registry.catalogs(binding) -> list[CatalogConfiguration]
registry.owner_by_name(text) -> Organisation | None                # names and aliases

# recognise
registry.recognise(url) -> ServiceBinding | None                   # longest endpoint_url prefix wins

# mint
registry.mint(kind, binding, code) -> str
    # urn:nordicintel:<kind>:<owner>:<code>             when owner code == source_code
    # urn:nordicintel:<kind>:<owner>:<source_code>:<code> otherwise
```

Invariants a caller must know:

- Loading validates every cross-reference once. After `load`, no method does I/O.
- `recognise` matches host, port and path prefix against `endpoint_url` entries only.
  There are no host names in code. If a URL is not served by a configured binding,
  the answer is `None` and the caller records a finding. The registry never invents
  identities.
- `mint` has one rule for every adapter. Adapters express their identity by what they
  emit as `source_code` and dataset code at parse time, not by hooks.
- `Organisation` is the registry's own plain model: `code`, `id`, multilingual
  `name`, `aliases`, `homepage`. It will be replaced by the model from
  `nordicintel/organisations` when that repository becomes the authority.

### Configuration changes that follow

- Riksbank's two databases (`swea`, `monetary_policy_data`) become two service
  bindings with their own catalogs. The riksbank adapter emits the database as its
  `source_code`. The URL regex goes.
- The two EntryScape tenants on shared hosting (digidel, klimatbyran) become their
  own service bindings with their context URL as `endpoint_url`. Catalog `scopes` are
  removed from the model and from `catalogs.json`; nothing else used them.
- Each organisation gains an `aliases` list. The `scb` and `rka` aliases move there
  from `matching.py`.
- The three SCB v1 hosts become endpoint URLs on the pxweb_v1 bindings that serve
  them.

### What is deleted

- `apps/configuration.py` and `apps/model/configuration.py` (moved into `registry/`).
- `apps/harvest/sources.py` entirely, including `inventory`, `deployment`,
  `source_entities`, `service_for_url`, `catalog_configurations`.
- `apps/migrations/` and `scripts/migrations/` entirely. Accepted collections are
  regenerated from source files, so profile upgrades and renames have no purpose.
  The audit reports under `docs/migrations/` are deleted too (amended by ADR 0004;
  git history is the record).
- Every reference to `entrysoft`, in code, tests, state and reports.
- `legacy_document` and `adapter_for` in parsing, `legacy_configuration`, the
  pxweb_v1-only half of `apps/harvest/storage.py`, and `migrate_storage`.
- The hardcoded host rules in `enrichment.py`, `matching.py`, `filtering.py`.

### What moves

- The accepted-document reader (`Dataset.from_jsonld` and nothing else) moves to
  `apps/parsing/accepted.py`, next to the writer. The catalog imports it from there.
- `JsonlStore`, `validate_dataset` and `refresh_identity` move into the pxweb_v1
  adapter.
- Adapters receive a `ServiceBinding` instead of a dict. ADR 0001 is amended.

### Identifiers are re-minted

Because every ID now follows one rule, kolada and entryscape IDs change. Accepted
data under `data/<adapter>/metadata/` is regenerated by parsing from the stored
source files, and the catalog is rebuilt. A diff report lists the changed IDs once.
Source files and manifests are untouched.

### Tests

- `tests/registry/` uses a small synthetic configuration: three bindings, two owners,
  two catalogs, one tenant on a shared host. Every behaviour test runs against it.
- One smoke test loads the real `config/` and checks it validates and that no two
  bindings recognise the same URL.
- Tests that asserted production counts (`len(adapters) == 12`) are deleted.

## Consequences

Good:

- One place answers identity questions; harvest, parsing and catalog agree by
  construction.
- No adapter name appears in shared code for identity purposes.
- Configuration is a parameter, not an ambient global. Tests build their own.
- Roughly 1,500 lines of migration, compatibility and duplicate lookup code go.

Costs:

- Kolada and EntryScape dataset IDs change once. Anything outside this repository
  that referenced them must be told.
- `catalogs.json` grows by four bindings and loses the `scopes` field.
- The portal matcher loses its substring tolerance for three publisher names unless
  those names are added as aliases, which they should be.

## Order of work

1. `registry/` with its synthetic-fixture tests and the production smoke test.
2. Configuration edits: riksbank and EntryScape bindings, aliases, SCB v1 endpoint
   URLs, drop `scopes`.
3. Replace the seven call sites; delete `sources.py`, `apps/configuration.py`,
   `apps/model/configuration.py`.
4. Move the reader; delete `apps/migrations/`, `scripts/migrations/`, every
   `entrysoft`.
5. Split `storage.py`; delete the rest of the legacy list.
6. Regenerate accepted collections and the catalog; record the ID diff.

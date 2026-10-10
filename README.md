# metadata-harvest

NordicIntel's API integration layer: importable adapters, shared output schemas,
validation, organisation, catalog and service configuration, metadata jobs and on-demand observation retrieval.
Only currently accessible source content is retained; there is no inactive store,
metadata history or observation warehouse.

## Coverage

| Adapter | Configured providers | Capabilities here |
|---|---|---|
| [PXWeb v1](docs/harvest/pxweb-v1.md) | 26 providers; see [catalogs.json](config/catalogs.json) | Website discovery, complete API metadata, standalone retrieval, resumable daily job |
| [PXWeb v2](docs/harvest/pxweb-v2.md) | SCB production API, `sv` and `en` | API discovery, complete metadata, independent codelists, standalone retrieval, daily incremental / Sunday full job |
| [Kolada](docs/harvest/adapters/kolada.md) | `kolada`, Swedish; municipality/groups and OU | Current metadata, targeted incremental refresh, standalone retrieval |
| [UKÃ„](docs/harvest/adapters/uka.md) | `uka`, Swedish; 113 indicators | Current metadata, weekly refresh reusing unchanged indicators, standalone retrieval |
| [SDB](docs/harvest/adapters/sdb.md) | `socialstyrelsen`, Swedish (15 subjects) and English (5) | Current metadata, weekly full rebuild, standalone retrieval |
| [Trafikanalys](docs/harvest/adapters/trafa.md) | `trafa`, Swedish; 457 datasets (temporary until the 2026 merger into TillvÃ¤xtanalys) | Current metadata, weekly full rebuild, standalone retrieval |
| [SMHI emissions](docs/harvest/adapters/smhi_emissions.md) | `smhi`, Swedish; 29 datasets (one per substance) | Current metadata, monthly full rebuild, standalone retrieval |
| [TillvÃ¤xtverket](docs/harvest/adapters/tillvaxtverket.md) | `tillvaxtverket`, sv; 6 datasets | Current metadata, weekly change-signal refresh, standalone retrieval |
| [Socialstyrelsen analysverktyg](docs/harvest/adapters/dataanalys.md) | `socialstyrelsen_analys`, sv; 94 accepted indicators in 6 areas | Current metadata, weekly full rebuild, standalone retrieval |
| [Riksbank](docs/harvest/adapters/riksbank.md) | `riksbank`, en; 42 datasets | Current metadata, weekly full rebuild, standalone retrieval |
| [FÃ¶rsÃ¤kringskassan](docs/harvest/adapters/forsakringskassan.md) | `forsakringskassan`, sv; 182 accepted datasets, 28 tables with undeclared dimensions excluded | Current metadata, weekly incremental (meta.json/texts hashes) and monthly full, standalone retrieval |
| [EntryScape RowStore](docs/harvest/adapters/entrysoft.md) | 49 EntryScape instances; see [catalogs.json](config/catalogs.json) | 442 datasets derived automatically from 531 RowStore tables, monthly budgeted refresh of changed tables, standalone retrieval with aggregation |

Metadata uses the extensible NordicIntel StatDCAT profile, defined by local Pydantic v2 models. Accepted files contain one multilingual dataset per line, grouped by organisation within each adapter. Distributions retain their independent structure, source identity and retrieval binding. Concrete DataServices reference shared adapter definitions; organisations can own catalogs using several adapters. See the [complete provider inventory](docs/configuration/inventory.md).

## Install and use

Python 3.13+. `pip install .` installs only the retrieval library. Its distribution
name remains `metadata-harvest`, with the import namespace `retrieval` and only
`aiohttp` as a runtime dependency. It is not currently published to PyPI.

```python
from retrieval import retrieve_distribution

# record is a decoded catalog JSONL entry; session is caller-owned aiohttp.
distribution = next(d for d in record["dcat:distribution"] if d.get("ni:retrieval"))
selection = {
    axis["code"]: axis["categories"][:1]
    for axis in distribution["ni:retrieval"]["dimensions"]
}
result = await retrieve_distribution(session, distribution, selection=selection)
```

Repository workflows live in `apps/`; retrieval implementation lives
in root `retrieval/`. Run `uv sync --locked --extra harvest` from this checkout
for the complete development/harvesting environment. Repository commands now use
`uv run python -m apps.operations.harvest ...` (see [operation commands](docs/operations/README.md)).
No manual `PYTHONPATH` setup is needed. The harvesting clients, Pydantic models,
configuration, accepted collections and publication logic remain repository code.

All existing `nordicintel.retrieval` entry-point names are preserved. Consumers of
old installed Python imports should use `retrieval` or `retrieval.data_adapters.<adapter>`.
Retrieval is imported exclusively from `retrieval`; harvesting has no retrieval aliases.
See [retrieval usage](docs/retrieval/README.md) and the
[dependency inventory](docs/retrieval/dependencies.md).

## Operations

Accepted adapter results live under `data/<adapter>/`; the shared consumer catalog
will be written by future production builds as `data/catalog/catalog.jsonl.zst`, with reference codes in
`data/catalog/codelists.jsonl.zst` and coverage/state alongside them. Each file is
a compressed JSONL stream. Existing `.7z` publication files remain readable until
production cutover. Zstandard belongs to the repository environment; consumers decompress archives separately. The catalog reads
committed accepted files only. Constructing it never starts a harvest, refreshes a
source, or resumes unfinished harvesting.

Publication triggers offline catalog assembly; a manual workflow is also available.
The separately refreshed, repository-owned oppnadata snapshot enriches matched
harvested datasets. SCB's main PXWeb v1 distributions are disabled by default in
`config/catalog.toml`; HVD and other providers are unaffected.
See [catalog processing](docs/catalog/README.md) for failure and promotion semantics.

[Schedules](config/schedules.toml) are the single source of automatic harvesting times,
timezone, modes and enablement. See [jobs and scheduling](docs/harvest/README.md) for
editing them, deliberate manual harvesting, and live counters/individual errors.
Adapters run independently; publication alone uses a shared queue.

[Consolidation evidence](docs/migrations/consolidation.md) separates implemented capabilities
from verified production coverage. Current adapter errors remain visible in the
catalog report; building a catalog never turns incomplete coverage into success.

## Documentation

- [Configuration and vocabulary](docs/configuration/README.md)
- [Architecture and ownership](docs/architecture.md)
- [Adapter services and canonical owners](docs/configuration/inventory.md)
- [Migration verification and preservation audit](docs/migrations/statdcat-audit/README.md)
- [Output contract and migration](docs/model/output-contract.md)
- [Legacy native mapping](docs/model/dataset-metadata.md) and [generated StatDCAT field reference](docs/model/reference.md)
- [Retrieval usage and adapter contract](docs/retrieval/README.md)
- [Adapter implementation guide](docs/harvest/adapter-guide.md)
- [Jobs, schedules and recovery](docs/harvest/README.md)
- [Catalog assembly and enrichment](docs/catalog/README.md)
- [Provider additions and HVD routing](docs/harvest/pxweb-registry-gaps.md)

## Verification

```powershell
uv sync --locked --extra harvest
uv run --extra harvest playwright install chromium
uv run --extra harvest ruff check .
uv run --extra harvest ruff format --check .
uv run --extra harvest python scripts/generate_schema_reference.py --check
uv run --extra harvest pytest
uv build
uv run --extra harvest python scripts/check_distribution.py
```

On Linux, browser installation uses `--with-deps`. Deterministic tests are separate
from explicit live acceptance. Imported schemas and their renderer retain the
[Apache-2.0 licence and provenance](schemas/README.md);
this does not assign a new licence to unrelated repository code.

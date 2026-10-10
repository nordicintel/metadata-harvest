# metadata-harvest

NordicIntel's API integration layer: importable adapters, shared output schemas,
validation, provider configuration, metadata jobs and on-demand observation retrieval.
Only currently accessible source content is retained; there is no inactive store,
metadata history or observation warehouse.

## Coverage

| Adapter | Configured providers | Capabilities here |
|---|---|---|
| [PXWeb v1](docs/pxweb-v1.md) | 26 providers; see [providers.json](providers.json) | Website discovery, complete API metadata, standalone retrieval, resumable daily job |
| [PXWeb v2](docs/pxweb-v2.md) | SCB production API, `sv` and `en` | API discovery, complete metadata, independent codelists, standalone retrieval, daily incremental / Sunday full job |
| [Kolada](docs/adapters/kolada.md) | `kolada`, Swedish; municipality/groups and OU | Current metadata, targeted incremental refresh, standalone retrieval |
| [UKÃ„](docs/adapters/uka.md) | `uka`, Swedish; 113 indicators | Current metadata, weekly refresh reusing unchanged indicators, standalone retrieval |
| [SDB](docs/adapters/sdb.md) | `socialstyrelsen`, Swedish (15 subjects) and English (5) | Current metadata, weekly full rebuild, standalone retrieval |
| [Trafikanalys](docs/adapters/trafa.md) | `trafa`, Swedish; 457 datasets (temporary until the 2026 merger into TillvÃ¤xtanalys) | Current metadata, weekly full rebuild, standalone retrieval |
| [SMHI emissions](docs/adapters/smhi_emissions.md) | `smhi`, Swedish; 29 datasets (one per substance) | Current metadata, monthly full rebuild, standalone retrieval |
| [TillvÃ¤xtverket](docs/adapters/tillvaxtverket.md) | `tillvaxtverket`, sv; 6 datasets | Current metadata, weekly change-signal refresh, standalone retrieval |
| [Socialstyrelsen analysverktyg](docs/adapters/dataanalys.md) | `socialstyrelsen_analys`, sv; 94 accepted indicators in 6 areas | Current metadata, weekly full rebuild, standalone retrieval |
| [Riksbank](docs/adapters/riksbank.md) | `riksbank`, en; 42 datasets | Current metadata, weekly full rebuild, standalone retrieval |
| [FÃ¶rsÃ¤kringskassan](docs/adapters/forsakringskassan.md) | `forsakringskassan`, sv; 182 accepted datasets, 28 tables with undeclared dimensions excluded | Current metadata, weekly incremental (meta.json/texts hashes) and monthly full, standalone retrieval |
| [EntryScape RowStore](docs/adapters/entrysoft.md) | 49 EntryScape instances; see [providers.entrysoft.json](providers.entrysoft.json) | 442 datasets derived automatically from 531 RowStore tables, monthly budgeted refresh of changed tables, standalone retrieval with aggregation |

Metadata uses the extensible NordicIntel StatDCAT profile, defined by local Pydantic v2 models. Accepted files contain one multilingual dataset per line, grouped by organisation within each adapter. Distributions retain their independent structure, source identity and retrieval binding. Exactly one shared DataService exists per adapter; organisations can own catalogs using several adapters. See the [complete provider inventory](docs/source-inventory.md).

## Install and use

Python 3.13+. Install this checkout with `pip install .` for retrieval, or
`pip install '.[harvest]'` for harvesting and schema validation.
The package is not currently published to PyPI.

```python
from metadata_harvest import Dataset, retrieve_distribution

# record is one decoded JSONL entry from catalog.7z.
dataset = Dataset.from_jsonld(record)
distribution = next(d for d in dataset.distributions if d.retrieval is not None)
selection = {
    axis.code: axis.categories[:1] for axis in distribution.retrieval.dimensions
}
result = await retrieve_distribution(session, distribution, selection=selection)
```

See the [working retrieval example](docs/retrieval-guide.md) and
[harvesting imports](docs/pxweb-v1.md). Plain imports do not load browser,
HTTPX or schema-validation dependencies. All implemented provider entry points
ship in this one distribution.

## Operations

Accepted adapter results live under `data/<adapter>/`; the shared consumer catalog
is committed as `data/catalog/catalog.7z`, with reference codes in
`data/catalog/codelists.7z` and coverage/state alongside them. Each archive
contains one JSONL file. Catalog construction requires 7-Zip. The catalog reads
committed accepted files only. Constructing it never starts a harvest, refreshes a
source, or resumes unfinished harvesting.

Publication triggers offline catalog assembly; a manual workflow is also available.
The separately refreshed, repository-owned oppnadata snapshot enriches matched
harvested datasets. SCB's main PXWeb v1 distributions are disabled by default in
`catalog.toml`; HVD and other providers are unaffected.
See [catalog processing](docs/catalog.md) for failure and promotion semantics.

[Schedules](schedules.toml) are the single source of automatic harvesting times,
timezone, modes and enablement. See [jobs and scheduling](docs/harvesting.md) for
editing them, deliberate manual harvesting, and live counters/individual errors.
Adapters run independently; publication alone uses a shared queue.

[Consolidation evidence](docs/consolidation.md) separates implemented capabilities
from verified production coverage. Current adapter errors remain visible in the
catalog report; building a catalog never turns incomplete coverage into success.

## Documentation

- [Architecture and ownership](docs/architecture.md)
- [Output contract and migration](docs/output-contract.md)
- [Dataset mapping contract](docs/DATASET-METADATA.md) and [generated field reference](docs/DATASET-METADATA-REFERENCE.md)
- [Retrieval contract](docs/RETRIEVAL.md) and [usage](docs/retrieval-guide.md)
- [Adapter implementation guide](docs/adapter-guide.md)
- [Jobs, schedules and recovery](docs/harvesting.md)
- [Catalog assembly and enrichment](docs/catalog.md)
- [Provider additions and HVD routing](docs/pxweb-registry-gaps.md)

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
[Apache-2.0 licence and provenance](src/metadata_harvest/schemas/README.md);
this does not assign a new licence to unrelated repository code.

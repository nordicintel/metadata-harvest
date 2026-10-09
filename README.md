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
| [UKÄ](docs/adapters/uka.md) | `uka`, Swedish; 113 indicators | Current metadata, weekly refresh reusing unchanged indicators, standalone retrieval |
| [SDB](docs/adapters/sdb.md) | `socialstyrelsen`, Swedish (15 subjects) and English (5) | Current metadata, weekly full rebuild, standalone retrieval |
| [Trafikanalys](docs/adapters/trafa.md) | `trafa`, Swedish; 457 datasets (temporary until the 2026 merger into Tillväxtanalys) | Current metadata, weekly full rebuild, standalone retrieval |
| [SMHI emissions](docs/adapters/smhi_emissions.md) | `smhi`, Swedish; 29 datasets (one per substance) | Current metadata, monthly full rebuild, standalone retrieval |
| [EntryScape RowStore](docs/adapters/entrysoft.md) | 49 EntryScape instances; see [providers.entrysoft.json](providers.entrysoft.json) | 442 datasets derived automatically from 531 RowStore tables, monthly budgeted refresh of changed tables, standalone retrieval with aggregation |

Metadata is one deterministic `.jsonl.gz` per adapter/provider/language under
`data/<adapter>/metadata/`; codelists have separate grouped collections.
Each Dataset uses `extension.nordicintel` identity, complete dimensions/categories
and `value: []`. V1 discards observations from bounded metadata slices; v2 does
not request observations. Standalone retrieval returns observations to its caller
and writes nothing. A failed codelist never invalidates an otherwise valid table.

## Install and use

Python 3.13+. Install this checkout with `pip install .` for retrieval, or
`pip install '.[harvest]'` for harvesting and schema validation.
The package is not currently published to PyPI.

```python
from importlib.metadata import entry_points


retrieve = entry_points(group="nordicintel.retrieval", name="scb")[0].load()

# Pass your aiohttp session, explicit category selection, and published metadata:

# await retrieve(session, provider_code=..., dataset_code=..., language=...,

#                selection=..., data_url=..., metadata_url=..., metadata=...)
```

See the [working retrieval example](docs/retrieval-guide.md) and
[harvesting imports](docs/pxweb-v1.md). Plain imports do not load browser,
HTTPX or schema-validation dependencies. All implemented provider entry points
ship in this one distribution.

## Operations

Accepted adapter results live under `data/<adapter>/`; the shared consumer catalog
lives under `data/catalog/`, grouped by adapter/provider/language. The catalog reads
committed accepted files only. Constructing it never starts a harvest, refreshes a
source, or resumes unfinished harvesting.

Publication triggers catalog construction automatically; a manual catalog workflow
is also available. Content enrichment runs before semantic enrichment. Both are
currently **not configured**, so trusted collections pass through byte-for-byte.
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
- [Catalog and enrichment placeholders](docs/catalog.md)
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

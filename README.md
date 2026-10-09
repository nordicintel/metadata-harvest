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
| SDB, Trafikanalys, RowStore, UKÄ | None enabled here | Explicitly unavailable packages and [migration preparation](docs/consolidation.md) |

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

## Operations and evidence

V1 runs daily at 04:15 Europe/Stockholm; SCB at 06:15, incremental Monday–Saturday
and full Sunday. Adapters harvest concurrently; only publication is serialized.
[Live counts and individual errors](docs/harvesting.md#inspect-a-running-job) are
available while workers run. All workflows support
manual execution and unfinished-work continuation. V1 uses `HARVEST_ENABLED`
and SCB uses `PXWEB_V2_ENABLED` as schedule controls. SCB's gate is complete table
acceptance; separate upstream codelist errors remain visible and retryable.
Kolada's incremental schedule is 06:30 Europe/Stockholm, gated by `KOLADA_ENABLED`.
Its initial trusted data requires no external repository at runtime.

Implementation, converted-corpus verification and production acceptance are
separate evidence. Consult [consolidation status](docs/consolidation.md) and the
current [v1 report](data/pxweb_v1/reports/harvest.json) /
[v2 report](data/pxweb_v2/reports/harvest.json); a failed run never means an empty
successful catalogue. Listing failures protect accepted records from deletion.

## Documentation

- [Architecture and ownership](docs/architecture.md)
- [Output contract and migration](docs/output-contract.md)
- [Dataset mapping contract](docs/DATASET-METADATA.md) and [generated field reference](docs/DATASET-METADATA-REFERENCE.md)
- [Retrieval contract](docs/RETRIEVAL.md) and [usage](docs/retrieval-guide.md)
- [Adapter implementation guide](docs/adapter-guide.md)
- [Jobs and recovery](docs/harvesting.md)
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

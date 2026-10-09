# metadata-harvest

Importable Python adapters and independent jobs for API metadata harvesting.
Output is grouped by adapter, provider and language. Only currently accessible
source content is kept: no inactive archive, metadata versions or dated snapshots.

## Implemented adapters

| Adapter | Configured coverage | Metadata retrieval | Output |
|---|---|---|---|
| [PXWeb v1](docs/pxweb-v1.md) | 26 providers, 52 databases, 62 language/database scopes | Website discovery, API GET and category-covering JSON-stat2 POST slices | `data/pxweb_v1/metadata/*.jsonl` |
| [PXWeb v2](docs/pxweb-v2.md) | SCB production API, Swedish and English | API-only discovery, complete native metadata and referenced codelists; no observations requested | `data/pxweb_v2/metadata/*.jsonl.gz` and `codelists/*.jsonl.gz` |

Each metadata collection contains one document per table with `value: []`.
V1 discards observations returned by bounded metadata slices. V2 never requests
observations. Valid v2 table metadata is accepted even when a referenced codelist
fails; codelist errors are reported and retried separately.

The [seven latest v1 additions](docs/pxweb-registry-gaps.md) are Göteborg, Karlstad,
Örebro, Helsingborg, Norrköping, Skogsstyrelsen SKA22 and **SCB HVD-uppdrag**.
HVD's hidden API parent is handled explicitly; all 32 tables passed full metadata
checks. The additions have passed complete discovery for 524 tables; metadata
checks for Göteborg and Karlstad were bounded. Socialstyrelsen's PXWeb API remains
unavailable and is not configured as supported. Exclusions are documented in the
[gap report](docs/pxweb-registry-gaps.md) and [excluded providers](docs/excluded-providers.json).

## Use from Python

```python
from metadata_harvest import get_pxweb_tables, get_pxweb_metadata

records = await get_pxweb_tables(
    "https://hvduppdrag.scb.se/pxweb/",
    languages=["sv"],
    database_ids=["uppdrag"],
)
document = await get_pxweb_metadata(records[0], provider_code="scb_hvd")
```

V2 exposes `Client`, `discover_provider`, `fetch_metadata` and `fetch_codelist`
from `metadata_harvest.adapters.pxweb_v2`; see its [shared-client example](docs/pxweb-v2.md).

## Automation and current status

| Workflow | Schedule (Europe/Stockholm) | Control |
|---|---|---|
| Scheduled harvest (v1) | Daily 04:15 | Enabled unless `HARVEST_ENABLED=false`; provider parallelism defaults to 19 |
| SCB PXWeb v2 metadata | 06:15; incremental Monday–Saturday, full Sunday | Scheduled runs require `PXWEB_V2_ENABLED=true`; manual runs expose `full` |

As checked on **9 October 2026**, v1 scheduling is enabled and v2 scheduling is
still disabled. Both workflows can run manually and share a publication queue.
The v2 harvest published all **8,620 tables** and **1,956 codelists**; its latest
run reports **55 upstream codelist failures**, independently of table success.
A subsequent incremental run reused every table and preserved all four compressed
collections byte-for-byte. The zero-unresolved-error gate for enabling the v2
schedule has not been met.

The latest committed v1 report predates the seven additions and remains incomplete;
configured and locally verified providers do not imply a completed production
harvest. The current machine-readable results are
[v1 harvest](data/pxweb_v1/reports/harvest.json),
[v1 discovery](data/pxweb_v1/reports/refresh.json),
[v2 harvest](data/pxweb_v2/reports/harvest.json) and
[v2 discovery](data/pxweb_v2/reports/refresh.json).

## Documentation

- [PXWeb v1 usage and live checks](docs/pxweb-v1.md)
- [PXWeb v2 usage, refresh rules and verification](docs/pxweb-v2.md)
- [Job operation, publication and recovery](docs/harvesting.md)
- [Output contract and file formats](docs/output-contract.md)
- [Implemented structure and remaining adapter migrations](docs/consolidation.md)
- [Latest provider additions and exact verification scope](docs/pxweb-registry-gaps.md)

## Development

Python 3.13+ and uv:

```powershell
uv sync --locked
uv run playwright install chromium
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

On Linux use `uv run playwright install --with-deps chromium`. Deterministic tests
run without live providers. Explicit live checks and production harvesting are
separate from CI. [Checks for the current implementation](https://github.com/nordicintel/metadata-harvest/actions/runs/37844771664)
passed; the verified suite contains 175 tests.

No PyPI publication or license has been configured.

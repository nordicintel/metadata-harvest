# Repository architecture

Only `retrieval/` is installed by the `metadata-harvest` distribution. Everything
else runs from this checkout with one root `pyproject.toml` and `uv.lock`. The
direction and the inventory of what the clean sweep removed are in
[clean-sweep.md](clean-sweep.md).

## Ownership

| Location | Responsibility |
| --- | --- |
| `registry/` | Load the three files under `config/` once, validate them, hand out service bindings and owner organisations |
| `harvest/lifecycle.py` | The loop around a harvest run: worker output, baseline copy, source store, state, retry, published or withheld, reports, `collect` |
| `apps/harvest/` | The source store, the publication rule (`currency.py`), logging, artifacts and storage helpers |
| `apps/harvest/metadata_adapters/<name>/` | One provider integration: `client.py`, `harvest.py` (`ADAPTER`), `parse.py`, optional `mapping.py` and private modules |
| `apps/parsing/` | Manual build and apply of accepted collections (worker documents) from source files |
| `apps/portal/` | Import, refresh and validate the accepted data.europa.eu snapshot |
| `apps/publication/` | Manifests, size checks and scoped Git publication |
| `apps/operations/` | CLI routing, scheduling, monitoring, progress and outcome reporting |
| `retrieval/` | The installable library: consumer observation retrieval per adapter |
| `config/` | Adapters, organisations, services and catalogs, settings and schedules |
| `schema/` | Schema seeds for the coming model: the worker-document schema, the configuration schema, merge and validation helpers |

## Data flow

```text
config/ -> registry -> ServiceBinding
ServiceBinding + adapter -> lifecycle run -> data/<adapter>/source + manifest + state + reports
source files -> parsing build/apply -> data/<adapter>/metadata (worker documents)
portal -> data/data_eu (snapshot read by the Tillväxtverket parse)
operations -> application commands
retrieval -> aiohttp + standard library + its pure helpers
```

Harvesting stores provider responses exactly as received. Parsing reads them later,
offline, and produces worker documents. A harvest never touches `metadata/`; a parse
never contacts a provider. Consumer retrieval owns protocol requests and observation
responses; harvesting may import identical pure helpers from `retrieval/` but never
its consumer functions, and the library never imports repository code.

Eight adapters run on the lifecycle's fetch-and-settle loop (dataanalys,
forsakringskassan, riksbank, sdb, smhi_emissions, tillvaxtverket, trafa, uka).
Kolada, EntryScape and PXWeb v2 keep loops of their own and use the lifecycle's
disk-handling half. PXWeb v1 keeps its multi-job pipeline.

## Commands and workflows

Run `uv sync --locked --extra harvest`, then `uv run python -m apps.operations.<module>
--help`.

| Module | Role |
| --- | --- |
| `harvest` | `run`, `collect` and `publish` for every adapter; the PXWeb v1 pipeline commands |
| `parsing` | Manual `build`, `apply` and `publish` of accepted collections |
| `portal` | Accepted snapshot import, refresh and publication |
| `scheduling` | Resolve schedules and synchronize workflow blocks |
| `monitor` | Live progress and individual error inspection |
| `outcome` | Report failure status for the selected adapter |

`checks.yml` runs lint, the fast tests, the generator check, the build and the
distribution check on every push; `slow-tests.yml` runs the full suite nightly. The
`harvest*.yml` workflows collect source files and `refresh-portal.yml` refreshes
the portal snapshot; their scheduled blocks are generated from
`config/schedules.toml`. `live-*.yml` run opt-in provider checks. Scripts live under
`scripts/{checks,generate,live}/` and run with `uv run python scripts/<purpose>/<tool>.py`.

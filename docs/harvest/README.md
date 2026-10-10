# Metadata harvesting

Harvesting adapters own discovery, native parsing, observation probes needed to
infer metadata, retries, pacing and checkpoints. They return typed contributions;
accepted collections contain multilingual StatDCAT datasets with independent source
distributions. Consumer retrieval is separate; see [architecture](../architecture.md).

## Run and collect

After `uv sync --locked --extra harvest`, inspect the selected adapter's arguments:

```sh
uv run python -m apps.operations.harvest --help
uv run python -m apps.operations.harvest run --adapter pxweb_v2 --data output/scratch-data --output output/pxweb-v2
uv run python -m apps.operations.harvest collect --adapter pxweb_v2 --data output/scratch-data --output output/pxweb-v2
```

`run` contacts sources and writes worker output; `collect` validates and applies it
locally; `publish` commits/pushes the selected production scope. PXWeb v1 instead has
separate refresh/build worker and collector commands. Use adapter help and workflow
arguments rather than constructing run identities by hand. Use a fresh output
directory for each new worker invocation; continuation reuses its checkpoint.

Accepted metadata is `data/<adapter>/metadata/<owner>__multilingual__<digest>.jsonl.gz`.
State, reports and unfinished work are adapter-owned. Reports distinguish failures
from reused or newly accepted records; observation probes do not create an
observation warehouse.

## Failure and recovery

Failed replacements retain accepted contributions independently. Incomplete discovery
or a missing worker is not deletion evidence. Only verified absence within a complete
scope authorizes removal. Some adapters use [currentness checks](../../apps/harvest/currency.py):
unreachable or stale documents are withheld in state and restored when eligible;
this is different from deleting their accepted evidence.

Resume unfinished work through the adapter checkpoint. Recover cancelled-worker
artifacts and validate them against their original baseline before collection;
do not bypass stale-artifact checks. Full refresh is an explicit adapter operation,
not a routine response to an unrelated development change.

## Scheduling and monitoring

[schedules.toml](../../config/schedules.toml) owns timezone, enablement and run modes.
After editing it, run `uv run python -m apps.operations.scheduling sync`, then
`sync --check`, and commit the generated workflow blocks with the configuration.
Disabling a schedule does not cancel existing jobs or disable manual runs.
`scheduling.paused = true` in [settings.toml](../../config/settings.toml) blocks
every scheduled run the same way; sync and commit after changing it too.
`logging.level` in the same file sets the log level shared by every metadata
adapter (`DEBUG`, `INFO`, `WARNING`, `ERROR`, or `OFF` to silence them); the harvest
entry point installs one stderr handler so adapter log lines appear in workflow
run output.

```sh
uv run python -m apps.operations.monitor status RUN_ID
uv run python -m apps.operations.monitor status RUN_ID --errors
uv run python -m apps.operations.monitor status RUN_ID --worker WORKER --error 1
uv run python -m apps.operations.outcome --adapter pxweb_v2
```

Actions workers publish counters and individual errors through a supervised GitHub
check before and during source requests. `reported_at` is the heartbeat;
`updated_at` is the last worker event. A heartbeat alone does not establish progress.
The inspector marks stale or ended unfinished checks explicitly. Telemetry failures
can stop workers while preserving snapshots; cancellation prevents automatic
continuation and collector publication.

Whole-scope failures fail jobs; individual resource errors can produce
`completed_with_errors` with accepted successes retained. Inspect the report rather
than equating a green job with complete coverage. Collection workflows serialize
production publication and may dispatch the offline catalog afterward; catalog
failure does not require another harvest.

## Adapter behavior

| Adapter | Notes |
| --- | --- |
| PXWeb v1 | [Website discovery and category-covering metadata probes](pxweb-v1.md) |
| PXWeb v2 | [Language catalogs and independent codelists](pxweb-v2.md) |
| Kolada | [KPI identities and separate municipality/OU scopes](adapters/kolada.md) |
| UKÄ | [Transient period exports](adapters/uka.md) |
| SDB | [Subjects, languages and result-field reconciliation](adapters/sdb.md) |
| Trafikanalys | [Product combinations and HTML discovery](adapters/trafa.md) |
| EntryScape RowStore | [Scoped discovery and budgeted refresh](adapters/entrysoft.md) |
| SMHI emissions | [Substances, sectors and region mappings](adapters/smhi_emissions.md) |
| Försäkringskassan | [Reports, tables and undeclared dimensions](adapters/forsakringskassan.md) |
| Socialstyrelsen analysverktyg | [Superset indicator/area scope](adapters/dataanalys.md) |
| Riksbank | [SWEA, forecasts and SWESTR](adapters/riksbank.md) |
| Tillväxtverket | [Explicit registry and complete change signals](adapters/tillvaxtverket.md) |

Current sources and endpoints are in the [generated inventory](../configuration/inventory.md).

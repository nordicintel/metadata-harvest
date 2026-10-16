# Metadata harvesting

A harvest stores provider responses as received: one envelope per dataset and
language plus shared listings under `data/<adapter>/source/`, indexed by
`manifest.json`. [Parsing](../parsing/README.md) turns those files into worker
documents later. Consumer retrieval is separate; see [architecture](../architecture.md).

## Run and collect

After `uv sync --locked --extra harvest`:

```sh
uv run python -m apps.operations.harvest --help
uv run python -m apps.operations.harvest run --adapter trafa --data output/scratch-data --output output/trafa
uv run python -m apps.operations.harvest collect --adapter trafa --data output/scratch-data --output output/trafa
```

`run` contacts the provider and writes a worker output; `collect` verifies it
against the unchanged published baseline and applies it; `publish` commits and
pushes the selected `data/<adapter>` scope. Each adapter declares the selection
flags it accepts (`--subject`, `--product`, `--indicator`, `--kpi`, `--scope`,
`--provider-code`, `--max-requests`, `--language`, `--full`); a selection bounds
the run and removes nothing. PXWeb v1 instead has separate refresh/build worker
and collector commands and continues across waves through its checkpoint.

The lifecycle writes `source/`, `manifest.json`, `state/current.jsonl.gz`,
`reports/latest.json` and `result.json`. State, reports and unfinished work are
adapter-owned. Where the per-dataset source data are observations (Kolada,
EntryScape, PXWeb v1 samples, UKÄ exports) the envelope keeps a declared `derived`
summary instead of the rows.

## Failure and recovery

A failed fetch keeps the stored source entry. An endpoint that does not answer is
retried once after the configured cooldown; an item whose endpoint stays down, or
that was not verified within `max_age_days`, is withheld in state and skipped by
parsing until it answers again (the rule is in
[currency.py](../../apps/harvest/currency.py)). Only a complete discovery in an
unbounded run removes an item. A whole-scope failure (a listing, a product, an
API) is reported once and keeps that scope's known items.

Recover cancelled-worker artifacts and validate them against their original
baseline before collection; do not bypass stale-artifact checks.

## Scheduling and monitoring

[schedules.toml](../../config/schedules.toml) owns timezone, enablement and run
modes. After editing it, run `uv run python -m apps.operations.scheduling sync`,
then `sync --check`, and commit the generated workflow blocks. `scheduling.paused`
and `logging.level` live in [settings.toml](../../config/settings.toml); every
adapter logs under its own `<adapter>_logger` name.

```sh
uv run python -m apps.operations.monitor status RUN_ID
uv run python -m apps.operations.monitor status RUN_ID --errors
uv run python -m apps.operations.outcome --adapter trafa
```

Workers publish counters and individual errors through a supervised GitHub check
while they run. Whole-scope failures fail jobs; individual resource errors produce
`completed_with_errors` with stored successes retained.

## Adapter behavior

| Adapter | Notes |
| --- | --- |
| PXWeb v1 | [Website discovery and category-covering metadata probes](pxweb-v1.md) |
| PXWeb v2 | [Language catalogs and independent codelists](pxweb-v2.md) |
| Kolada | [KPI identities and separate municipality/OU scopes](adapters/kolada.md) |
| UKÄ | [Transient period exports](adapters/uka.md) |
| SDB | [Subjects, languages and result-field reconciliation](adapters/sdb.md) |
| Trafikanalys | [Product combinations and HTML discovery](adapters/trafa.md) |
| EntryScape RowStore | [Budgeted refresh over many hosts](adapters/entryscape.md) |
| SMHI emissions | [Substances, sectors and region mappings](adapters/smhi_emissions.md) |
| Försäkringskassan | [Reports, tables and undeclared dimensions](adapters/forsakringskassan.md) |
| Socialstyrelsen analysverktyg | [Superset indicator/area scope](adapters/dataanalys.md) |
| Riksbank | [SWEA, forecasts and SWESTR](adapters/riksbank.md) |
| Tillväxtverket | [Explicit registry and complete change signals](adapters/tillvaxtverket.md) |

Current sources and endpoints are in the [generated inventory](../configuration/inventory.md).

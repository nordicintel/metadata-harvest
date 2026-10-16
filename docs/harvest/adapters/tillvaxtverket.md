# Tillväxtverket

Implementation: `apps/harvest/metadata_adapters/tillvaxtverket/`.

## Explicit registry and signals

DivePort has no usable dataset listing; `source.py` owns the supported lodging
dataset registry and column roles. Adding a dataset is a code/configuration review,
not open-ended discovery. Unexpected columns fail metadata validation.

The complete change signal combines columns/rowCount (`limit=0`) with every distinct
`LOAD_DATE`. A single sample row is insufficient because geographic levels and
periods can have different load times. Reuse unchanged datasets only after comparing
that signal. Variable labels come from Windows-1252 semicolon-delimited CSVs, with
native column-name fallback. Portal translations remain evidence rather than
invented English source distributions.

## Geography and limitations

Retain national, county and municipality identities and source-suppressed categories.
Unknown/suppressed codes must not collapse unrelated geographic rows. Keep source
hierarchies and exact category order. The server's aggregation semantics are not
established, so dimensions are not eliminable and every dimension must be selected.

Retrieval separates regional levels, uses simple column filters and splits oversized
URLs. Pagination must agree with rowCount; incomplete or changing counts fail rather
than returning partial observations. Integral numeric values remain integers where
the source column declares them. Suppression markers become status and zeroes remain
zeroes, even where their interpretation is uncertain.

Currentness/withholding uses working endpoints and `max_age_days`. Unknown measure
units, `SoL` semantics and whether national rows equal geographic sums remain open
source questions. Do not manufacture units or aggregation behavior.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../../migrations/README.md).

## Stored source data

Each dataset's envelope holds the column, load-date, region, dimension and period responses with the label file as a companion; the data.europa.eu description is applied by `parse` from the accepted portal snapshot.

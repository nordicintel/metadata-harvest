# Försäkringskassan

Implementation: `apps/harvest/metadata_adapters/forsakringskassan/`.

## Reports and tables

`/reports` lists reports; each `/{report}/meta.json` lists dimensions, measures and
breakdown tables. A dataset is a table within a report. Dimensions with a `group`
belong only to the matching breakdown; ungrouped dimensions are shared. Swedish
texts come from the separate text service, decoded without losing escaped content.
English requests do not provide a separate translated source.

When table ordering names an undeclared dimension, probe the individual table.
Exclude tables whose returned rows contain unmodelled keys, recording
`undeclared_dimensions`; valid siblings remain eligible. Do not silently collapse
an undisclosed geographic or diagnosis dimension. UI selection defaults are not
complete metadata, and XLSX exports are not required for this adapter.

## Change signals and failures

Incremental refresh compares metadata/text hashes; full refresh can rebuild even
when those signals are unchanged. Keep source-backed units, marker semantics,
complete codes and source evidence. Incomplete or malformed discovery cannot remove
accepted tables. Previously failed work remains retryable. Endpoint availability
and `max_age_days` determine whether accepted documents are published or withheld.

Consumer retrieval uses each table's retained column/breakdown mapping and validates
returned rows against the selection. Undeclared source columns are a scope problem,
not permission to manufacture categories or discard dimensions. See current reports
for exclusions instead of relying on a fixed historical table count.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.

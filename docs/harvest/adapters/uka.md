# UKÄ

Implementation: `apps/harvest/metadata_adapters/uka/`.

## Exports and categories

Each indicator is a dataset. UKÄ's listing does not contain complete categories,
so harvesting reads period CSV exports transiently and discards observations.
The first column defines time, the last the measure, and intermediate columns
define breakdowns. `Name[0]`, `Name[1]`, … form hierarchical categories whose codes
join paths with ` > `. Blank or Total breakdowns represent the eliminated total.

Only periods with usable exports enter coverage. Retain period export URLs,
Last-Modified values, source records, units and marker meanings. Swedish category
labels are codes; renaming a label changes its category identity.

## Refresh and retrieval

Compare source records, export links and mapping version; unchanged exports can be
reused after their freshness checks. Currentness requires a working endpoint and
verification within configured `max_age_days`. Otherwise retain the document as
withheld state. A mapping error on a working endpoint retains the previous document
while fresh. Only complete, unbounded discovery establishes removal.

Consumer retrieval downloads selected periods, with bounded concurrency; omitted
breakdowns select Total rows. Markers become null plus status, absent cells remain
absent, and malformed/conflicting exports fail. A selection without a usable period
is invalid. The pure CSV parser is shared from `retrieval/_uka_export.py`; harvest
probes do not call the consumer retrieval function.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../../migrations/README.md).

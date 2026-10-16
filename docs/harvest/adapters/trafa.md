# Trafikanalys

Implementation: `apps/harvest/metadata_adapters/trafa/`; routing alias `trafa`.

## Dataset boundaries

Each maximal API combination of a product is a dataset. Identity combines the native
product ID with sorted measure and variable names. Product documentation HTML lists
valid combinations absent from `/structure`; this adapter deliberately uses both.
Swedish labels define configured coverage. The test product `ATFT` is excluded unless
explicitly requested.

Measures form a synthetic measure dimension. A bounded `/data` probe supplies units;
harvesting discards its observations. Preserve nested variable hierarchies, original
combinations and source attribution. The eliminated total category is `t1`.

## Refresh and constraints

No reliable source change signal exists, so each run rebuilds selected products.
Use `--product T10011 T10033` for a bounded run. Metadata failures retain accepted
documents and do not prove removal. Endpoint availability and `max_age_days` govern
withholding; only complete unbounded discovery establishes absence.

Consumer retrieval splits queries around URL/row limits, preserves markers and
notes in status, and requires each dimension, using `t1` for explicit totals.
If the source API changes or retires, report the failure and review configuration;
an outage does not authorize deleting accepted data.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.

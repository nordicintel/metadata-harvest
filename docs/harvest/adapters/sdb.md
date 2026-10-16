# Socialstyrelsen SDB

Implementation: `apps/harvest/metadata_adapters/sdb/`; routing alias `socialstyrelsen`.

## Subject and language scope

Each native subject is one dataset with language-specific distributions. Variables
retain API ordering and category IDs as strings. Classification parents preserve
hierarchies, including DRG/MDC and hospital/county relationships. Missing English
labels stay missing rather than receiving Swedish replacements.

A first-result-row probe reconciles metadata variables with result fields: for
example `yttreorsak` may be returned as `diagnos`. Preserve this result-dimension map
for retrieval and add observed years missing from the year listing with evidence.
The Swedish listing supplies time interpretation; its failure blocks a normal run.
Other language scopes are independently handled.

## Refresh and limitations

SDB has no usable modification signal, so runs rebuild subjects and reuse identical
accepted content. A working result endpoint and verification within `max_age_days`
govern publication; otherwise retain withheld state. Mapping errors on working
endpoints preserve still-fresh documents. Only complete, unbounded discovery removes
subjects; malformed or duplicate rows block that decision.

Bound work using `--subject amning dodsorsaker --language sv`. Retrieval must use
the retained result-field mapping. Separate Socialstyrelsen PXWeb and Superset
installations are different sources; do not infer their coverage from SDB subjects.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.

# EntryScape RowStore (`entrysoft`)

Implementation: `apps/harvest/metadata_adapters/entrysoft/`.

## Scope and inference

Keep hosting endpoints, discovery origins, catalog/context membership and publisher
identities separate. Shared hosts can serve multiple organisations; configured scopes
control membership. See [configuration](../../configuration/README.md).

Inference runs from accepted table structures: constants and code-like identifiers
remain evidence; detected code/label pairs share a dimension; high-cardinality columns
become filters. Compatible period tables or distributions of one catalog dataset may
combine using a `part` dimension. Numeric columns become measures; row-only datasets
use a row-count measure. Blank values use `(tom)`. Tables without a usable dimension,
test tables and single-row tables are excluded by mapping rules.

## Budgeted updates

Preserve the imported baseline. Changed modification markers trigger harvesting;
tables without markers use a minimal row-count check. Offline mapping-version changes
can regroup saved structures without fetching unchanged tables. The shared pacer,
request budget, pause after the first 429 and stop after repeated 429s prevent
repeated expensive harvests. Completed work survives a budget stop.

Discovery failures retain scoped tables; only complete, unbounded discovery can
remove them. Unsupported attribution retains evidence and findings. Use
`--provider-code catalogalese --max-requests 40` for deliberately bounded work.

## Consumer limitations

Retrieval pages selected member tables and sums matching numeric rows; omitted
dimensions collapse into aggregate cells. `cells_unique: false` warns that shares
or averages may produce meaningless sums. Rows without observations stay sparse.
Category labels are native values; renamed values become new identities. Code/label
pair inference is heuristic and large deferred tables require a deliberate budget.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../../migrations/README.md).

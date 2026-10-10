# EntryScape RowStore (`entrysoft`)

## Metadata contract

Public `fetch_metadata` returns typed `HarvestContribution` models. Accepted metadata
uses provider-grouped multilingual StatDCAT datasets; distribution retrieval bindings
retain the original source alias, language and scope. Each access service identifies a concrete API and references its adapter; publisher
relationships are resolved separately for catalogs, services and datasets. See the [profile and field
mapping](../output-contract.md), [owner inventory](../source-inventory.md) and
[typed retrieval](../retrieval-guide.md).

The native field names, JSON-stat examples and provider entry-point calls below
describe adapter parsing and transition compatibility, not the published JSON-LD
shape. Dated acceptance sections record their original verification.

The `harvesting.adapters.entrysoft` package publishes the RowStore tables of
Swedish EntryScape catalogues as queryable datasets. EntryScape (MetaSolutions)
exposes an uploaded CSV file as a paged JSON API, `https://{instance}/rowstore/dataset/{uuid}`,
with per-column filters. Agencies and municipalities use it to publish tabular
statistics without an API of their own.

- Ported from the proof of concept in the `Third-party-APIs` workbench
  (`sources/entryscape-rowstore/adapter`, commit `f139c99`), whose harvested
  results are the accepted baseline here.
- One provider per EntryScape instance: 49 providers in
  [catalogs.json](../../catalogs.json), each with its own
  retrieval entry point.

## Accepted baseline and rate limits

The baseline was imported with `scripts/import_entrysoft.py` from a harvest that
cost thousands of requests. The hosts now rate-limit us, so **no run repeats that
harvest**: tables are reharvested only when their catalogue `modified` markers
change, a table without markers costs one `_limit=1` request to compare its row
count, and an unchanged table costs nothing.

Every request goes through one pacer: one request per second across all hosts, a
budget per run (1,500 by default, `max_requests` on manual runs), a 30-second pause
after the first HTTP 429 and a stop at the second. A stopped run keeps every finished
table; the next run continues. The schedule is monthly, the first of the month at
06:30 Europe/Stockholm. Live checks run from a GitHub runner, never from a host the
instances already block.

## From tables to datasets

A RowStore table is not a dataset. Everything below is decided automatically from
the harvested structure (`datasets.py`), so a new table gets the same treatment.

Column rules, per table:

| Column | Becomes |
|---|---|
| One distinct value | Dropped; kept under `extension.entrysoft.constants` |
| Code-like and distinct in at least half the rows (ids, invoice numbers) | Dropped; kept under `identifiers`. If nothing else is left, the identifiers are the dimensions |
| Same distinct count as a code column with a shared name stem (`konto_nr`/`konto_text`) | Folded into the code column as `label_column` |
| Time values, more than 1,000 of them | A filter with a `range`, not a dimension |
| More than 1,000 distinct values | A filter (`enumerable: false`), not a dimension |
| Otherwise | A dimension with every category listed |

Table rules, per provider: tables with identical columns whose titles differ only by
a period, or that are distributions of the same catalogue dataset, merge into one
dataset with a `part` dimension whose categories map to table URLs. Tables titled
as tests and single-row tables are dropped. A dataset with no dimension left is a
list and is not published.

The rules run offline from `state/tables.jsonl.gz` on every run, so bumping
`MAPPING_VERSION` regroups everything without a request.

## Output

`data/entrysoft/metadata/<owner>__multilingual__<digest>.jsonl.gz`, one collection per
provider, 442 datasets as of the import. Each document:

- `dataset_code` is the table uuid, or `<provider>:<title stem>` for a merged dataset
  (with `#<digest>` when two merged groups share a stem).
- Listed columns and the synthetic `measure` dimension form the cube. Measure
  categories are the numeric column names; a dataset without numeric measures has
  the single measure `rows`, the row count (`measure_kind: "row_count"`).
- **Every column dimension is eliminable** (`elimination: true`, no value).
- Blank cell values share the category code `(tom)`.
- `extension.entrysoft`: `kind`, `tables`, `rowcount`, `filters`, `identifiers`,
  `constants`, `cells_unique`, `source_record` (the member tables' catalogue
  records), `updated_original`.
- `time_unit` is read from the time dimension's codes.

`state/tables.jsonl.gz` holds every discovered table with its discovery record,
markers, status (`ok`, `empty`, `deferred`, `error`), cost and, for harvested tables,
the trimmed structure (full category lists up to 1,000 values; otherwise the count, a
sample and the range). `reports/latest.json` lists the deferred tables with the
paging cost that harvesting them would take.

## Retrieval

The 49 entry points resolve to `retrieval.adapters.entrysoft:retrieve`,
with `REQUIRED_METADATA = ("/id", "/dimension", "/extension/entrysoft")`.

- Rows matching the selection are read from the tables the `part` selection covers
  (all tables when `part` is omitted), 500 per page, at most 50 requests.
- Cells sum the numeric measures of the rows behind them: dropped identifiers,
  unlisted wide columns and omitted dimensions all collapse into their cell.
  `status` reports `rows=N` when a cell covers more than one row, and the text of a
  non-numeric value, which becomes `null`.
- Cells without rows are left out (sparse `value`).
- `ValueError` for unknown parts or selections needing more than 50 requests,
  `LookupError` for a table that no longer exists.

## Operations

```powershell
uv run --extra harvest python -m harvesting.runner run --adapter entrysoft --provider-code catalogalese --max-requests 40
uv run --extra harvest python -m harvesting.runner collect --adapter entrysoft
uv run --extra harvest python scripts/check_entrysoft_live.py --provider catalogalese   # from a GitHub runner only
```

A run fails before any request when three reference hosts' search endpoints do not
answer. A provider whose discovery fails keeps its tables; only a complete discovery
in an unbounded run removes tables no longer listed. Three unexpected table failures
stop a run so that a bug does not repeat across hundreds of tables.

## Verification

```powershell
uv run --extra harvest pytest tests/entrysoft
uv run --extra harvest python scripts/import_entrysoft.py <rowstore_final> --data <scratch>   # offline: 442 documents, grouping equals the proof of concept
```

## Limitations

- Category labels are the cell values; a renamed value is a new category.
- Code/label pairs are detected by name and distinct count only.
- Summing is wrong for share or average measures in datasets whose cells cover
  several rows (`cells_unique: false`); consumers should check that flag.
- 17 large tables without usable source files are deferred; harvesting them costs
  the listed number of paging requests and must be chosen deliberately.
- Two instances, STUNS and Falun, have never been harvested.

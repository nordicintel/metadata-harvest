# UKÄ

## Metadata contract

Public `fetch_metadata` returns typed `HarvestContribution` models. Accepted metadata
uses provider-grouped multilingual StatDCAT datasets; distribution retrieval bindings
retain the original source alias, language and scope. Each access service identifies
this adapter, independently of the owning organisation. See the [profile and field
mapping](../output-contract.md), [owner inventory](../source-inventory.md) and
[typed retrieval](../retrieval-guide.md).

The native field names, JSON-stat examples and provider entry-point calls below
describe adapter parsing and transition compatibility, not the published JSON-LD
shape. Dated acceptance sections record their original verification.

The `metadata_harvest.adapters.uka` package implements Swedish metadata and
on-demand retrieval for UKÄ's undocumented statistics indicators API,
`https://statistik-api.uka.se/api`. It was ported from
[uka-adapter `ca533a7`](https://github.com/nordicintel/uka-adapter/tree/ca533a715e7774a757056afb7cf55db80092f4dd),
keeping its mapping. There is no runtime dependency on that repository.

## Scope decision: exports are read transiently

A complete list of an indicator's categories exists only in UKÄ's per-period CSV
exports (`/api/totals/{id}?export=all&export_type=csv&year=<period>` answers a file
link). `/api/totals/{id}` gives only one-way marginals and uses category ids that
the exports do not carry. The harvest therefore downloads every export of a *changed*
indicator, derives the dimensions and categories in memory and then discards the
file. No export, cache or observation is stored. Retrieval reads the same exports
on demand. A full rebuild is about 3,300 files and 3 GB, so unchanged indicators
are never downloaded again.

## Output

`data/uka/metadata/<owner>__multilingual__<digest>.jsonl.gz` holds one Dataset per published catalogue
indicator. The provider is `uka`, the `dataset_code` is the indicator id, and the
language is `sv` (UKÄ publishes only Swedish). `state/current.jsonl.gz` holds:

- for each indicator: its export links, its source digest, its mapping version,
  `verified_at`, `checked_at`, its publication status and its last error;
- the withheld documents (see below).

`reports/latest.json` holds the latest counts and the individual errors.

Mapping decisions, unchanged from the external adapter:

- The first export column is the time dimension (`Termin`, `Läsår`, `År`).
- The last column is the single measure.
- Every column in between is a breakdown. `Name[0]`, `Name[1]`, … columns form one
  hierarchical dimension, whose category codes are paths joined by ` > `.
- A blank breakdown cell or `Total` is the eliminated `Total` category.
- `Län` and `Land` get the geo role.
- Markers such as `..` and conflicting duplicate cells are listed in
  `extension.uka`.
- `extension.uka.files` records every period's export URL and `Last-Modified`.
- `extension.uka.source_record` holds the catalogue row and the totals metadata.
- Root `updated` is the newest export `Last-Modified` date.
- Periods UKÄ lists without an export are left out of the time dimension.

## Weekly refresh

The workflow runs every Monday at 05:15 Europe/Stockholm, as configured in
[schedules.toml](../../schedules.toml). A run takes minutes when little changed.

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter uka
uv run --extra harvest python -m metadata_harvest.runner collect --adapter uka
# Bounded run or forced rebuild of every indicator:
uv run --extra harvest python -m metadata_harvest.runner run --adapter uka --indicator 97 15
uv run --extra harvest python -m metadata_harvest.runner run --adapter uka --full
```

A run proceeds as follows:

1. **Catalogue.** Read the catalogue. If it is unreachable or empty, the run fails
   and nothing is published.
2. **Per indicator.** Read the indicator's totals and every period's export link.
3. **Reuse or rebuild.** If the links, the source record and `MAPPING_VERSION` are
   unchanged, reuse the accepted document after a HEAD check of the newest export.
   Otherwise download that indicator's exports and rebuild and validate its
   document.
4. **Retry.** Requests are retried with backoff. Indicators whose endpoint failed are
   retried once more at the end of the run.

An indicator is **published** only when both conditions hold:

1. its value endpoint (totals, export links and the export files) works in this
   run, and
2. its document was verified, meaning rebuilt or confirmed unchanged, within
   `max_age_days` (60, set in [providers.uka.json](../../providers.uka.json)).

Otherwise the document is **withheld**: it is removed from the published collection
but kept in state, and it is restored automatically once both conditions hold
again. A mapping or validation failure on a working endpoint keeps the previous
document published until it becomes older than the limit. Only a complete catalogue
in an unbounded run removes an indicator. Malformed catalogue rows make discovery
incomplete, which blocks removal. Errors produce `completed_with_errors`; successful
indicators are still published.

## Retrieval

The `uka` entry point works in a plain installation and needs only `REQUIRED_METADATA = ("/role",)`:

```python
from metadata_harvest.adapters.uka.retrieval import retrieve

result = await retrieve(
    session,
    provider_code="uka",
    dataset_code="97",
    language="sv",
    selection={"Termin": ["HT2025"], "Kön": ["Kvinnor", "Män"]},
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"]["metadata_url"],
    metadata={"/role": document["role"]},
)
```

Each selected period costs one link request and one file download, with at most
four periods fetched at once. An omitted breakdown answers its `Total` rows.

The result format:

- Markers become `null` values with the marker in `status`.
- Cells absent from the export are left out.

Errors:

- `ValueError`: the selection has no period, or a selected period has no export.
- `LookupError`: the indicator is gone.
- `RuntimeError`: UKÄ's export is malformed or conflicting.

Transport errors propagate.

## Verification

```powershell
uv run --extra harvest pytest tests/test_uka_export.py tests/test_uka_source.py tests/test_uka_mapping.py tests/test_uka_client.py tests/test_uka_retrieval.py tests/test_uka_workflow.py
uv run --extra harvest python scripts/check_uka_live.py
```

The fixtures are uka-adapter's recorded responses. The live script rebuilds and
validates three indicators and retrieves each one's newest period, saving no
observations.

UKÄ's reuse terms, citation wording and API stability are undocumented. Category
codes are UKÄ's Swedish label texts, so a renamed category becomes a new category.

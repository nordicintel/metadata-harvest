# SDB (Socialstyrelsen)

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

The `metadata_harvest.adapters.sdb` package implements Swedish and English metadata
and on-demand retrieval for Socialstyrelsen's statistics database API,
`https://sdb.socialstyrelsen.se/api/v1`.

- It was ported from
  [sdb-adapter `344212d`](https://github.com/nordicintel/sdb-adapter/tree/344212d51a28b848288e2beb90b7a5432bfa225e),
  keeping that adapter's mapping.
- There is no runtime dependency on that repository.
- The provider code is `socialstyrelsen`.

## Output

Both languages share the collection: `data/sdb/metadata/<owner>__multilingual__<digest>.jsonl.gz`
with language-specific distributions. Each SDB subject is one dataset; its native
code is the subject name. Swedish has 15 subjects and English 5. Discovery and
acceptance remain independent per language, including partial failures.

`state/current.jsonl.gz` holds, for each `<language>/<subject>`:

- `verified_at`, `checked_at`, publication status and last error;
- the withheld documents.

`reports/latest.json` holds the latest counts and the individual errors.

### Mapping

The mapping is unchanged from the external adapter.

- **Variables.** Each SDB variable is a dimension, in API order. Category codes are
  SDB ids as strings.
- **Hierarchies.** Classification variables (those with `typ`) become one dimension
  whose `grupp` parents form `category.child`. DRG groups nest under their MDC, and
  DRG hospitals under their county.
- **Other source fields.** Further SDB category fields are kept under
  `dimension.<id>.extension.socialstyrelsen.categories`. HTML `info` texts become
  notes.
- **Roles.** `ar` and `manad` get the time role, `region` geo and `matt` metric.
- **Time unit.** `time_unit` comes from the Swedish subject name. Multi-year windows
  use `other`.
- **Untranslated labels.** English labels missing in SDB are omitted, never filled
  from Swedish.
- **Result probe.** The subject's first result row is sampled, which reconciles the
  listing with the data API.
  - Renamed result fields are recorded in
    `extension.socialstyrelsen.result_dimensions`. For example, the injury subjects
    report `yttreorsak` as `diagnos`.
  - Years that have data but are missing from the `ar` listing are added and marked
    `from_results`.
- **Source evidence.** The subject's listing row is kept in
  `extension.socialstyrelsen.source_record`.

## Weekly refresh

The workflow runs every Monday at 05:45 Europe/Stockholm, as configured in
[schedules.toml](../../schedules.toml). SDB publishes no modification dates, and a
full harvest is about 200 requests and under two minutes, so **every run rebuilds
every subject**. An identical rebuild keeps the accepted bytes and is counted as
`unchanged`.

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter sdb
uv run --extra harvest python -m metadata_harvest.runner collect --adapter sdb
# Bounded runs:
uv run --extra harvest python -m metadata_harvest.runner run --adapter sdb --subject amning dodsorsaker --language sv
```

A run proceeds as follows:

1. **Swedish listing.** The Swedish subject listing is required, because it supplies
   the time units. If it is unreachable or empty, the run fails and nothing is
   published.
2. **Other listings.** Every other language's listing is independent. If it fails,
   that language's subjects are withheld while Swedish proceeds.
3. **Subjects.** Each subject is rebuilt in each language. Requests are retried, and
   subjects whose endpoint failed are retried once more at the end of the run.

A subject is **published** only when both conditions hold:

1. its result endpoint works in this run. The result probe's first-row request is
   the endpoint check: a 404, no rows or a transport failure means not working.
2. its document was verified within `max_age_days`. That is 60 days, set in
   [providers.sdb.json](../../providers.sdb.json).

Otherwise the document is **withheld**: it is kept in state and restored
automatically once both conditions hold again.

Further rules:

- A mapping or validation failure on a working endpoint keeps the previous document
  published until that document is older than the limit.
- Only a complete listing in an unbounded run removes a subject, and only from that
  language. Malformed or duplicate listing rows block removal.
- Errors produce `completed_with_errors`; successful subjects are still published.

The publication rule is shared with UKÄ in `metadata_harvest.currency`.

## Retrieval

The `socialstyrelsen` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/extension/socialstyrelsen/result_dimensions",)`:

```python
from metadata_harvest.adapters.sdb.retrieval import retrieve

fragment = await retrieve(
    session,
    provider_code="socialstyrelsen",
    dataset_code="dodsorsaker",
    language="sv",
    selection={
        "region": ["0"],
        "alder": ["18"],
        "kon": ["1", "2"],
        "matt": ["1"],
        "ar": ["2023"],
        "diagnos": ["99"],
    },
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"]["metadata_url"],
    metadata={"/extension/socialstyrelsen/result_dimensions": result_dimensions},
)
```

Request behaviour:

- Code lists are split so that each path segment stays within 250 characters.
- Pages hold 5,000 rows.
- At most four requests run concurrently.
- Requests are never retried.

Results:

- `value` and `status` are sparse. Non-numeric `varde` texts such as `X` become null
  values, with the text kept in `status`.

Errors:

- Every dimension must be selected; an omitted dimension raises `ValueError`.
- A removed subject raises `LookupError`.
- Rows that do not match the request raise `RuntimeError`.

## Verification

```powershell
uv run --extra harvest pytest tests/sdb
uv run --extra harvest python scripts/check_sdb_live.py
```

The tests run against an in-process SDB imitation built from sdb-adapter's trimmed
recordings. The live script rebuilds and validates `sv/amning`, `sv/dodsorsaker` and
`en/dodsorsaker`, and retrieves one small selection from each. It saves no
observations.

## Limitations

- **Dimensions.** No dimension is eliminable.
- **Missing metadata.** There is no `updated`, `source_url` or `doc_url`, because SDB
  exposes no dates.
- **Flags.** The meaning of SDB's flags is undocumented.
- **Years.** Years missing inside the listed range are not probed.

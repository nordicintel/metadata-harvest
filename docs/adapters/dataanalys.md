# Socialstyrelsen analysverktyg

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

The `harvesting.adapters.dataanalys` package implements metadata and on-demand
retrieval for Socialstyrelsen's analysverktyg, a public Apache Superset instance at
`https://dataanalys.socialstyrelsen.se` with health-care indicators such as
"Indikatorer för god och nära vård".

- The provider code is `socialstyrelsen_analys`. It is distinct from
  `socialstyrelsen`, which the [SDB adapter](sdb.md) owns.
- Native metadata lives in `extension.socialstyrelsen_analys`.
- Swedish only: the site has no English content.
- The source research is in the local, git-ignored folder
  `docs/api/socialstyrelsen-superset/` (README, REPORT, samples, recorded
  2026-10-09).

## Scope decisions

- **One dataset per indicator and area.** An indicator is one row of an area's
  catalogue table, `semantic_VAI_{AREA}_Landningssida`, identified by
  `MBusinessKey`, which is `Matt_id` in the area's values table,
  `semantic_VAI_{AREA}_Trender`. The dataset code is `{AREA}|{MBusinessKey}`, for
  example `GONV|G082`. Superset datasets, charts and dashboards are not datasets.
- **Areas.** Areas are found by grouping the chart list's datasource names with
  `^semantic\.semantic_VAI_(?P<area>[A-Za-z0-9]+)_(Landningssida|Trender)$`.
  - *Deviation:* the `semantic.` schema prefix is matched case-insensitively,
    because NRTV's catalogue is listed as `Semantic.semantic_VAI_NRTV_Landningssida`.
  - An area with only one of the two tables is reported in `areas_incomplete`.
  - On 2026-10-09 there were seven areas: GONV, LRHST, NPS, NRAA, NRTV, OB and PS.
- **Out of scope.**
  - `MART.*` tables (Cancer i Sverige, NKÄ/äldreomsorg) and the other
    non-semantic datasources are listed in `unsupported_datasources`. They have
    their own layouts.
  - **NPS** (Nationella planeringsstödet) is reported in `areas_unsupported`. Its
    values table has `Mbusinesskey` and a profession breakdown that the indicator
    model has no dimension for.
  - **Geography: Riket and Region only.** Kommun, care-unit (`Enhet`) and other
    levels (for example NRAA's `Områdestyp`) are excluded from documents and
    retrieval. Region categories are Socialstyrelsen's names (`Stockholm`,
    `Västra Götaland`), not SCB codes.
  - An indicator without Riket or Region rows is counted as `out_of_scope` and not
    published.
- **Values-table column names differ between areas.** This was found live on
  2026-10-09:

  | Areas | Columns |
  | --- | --- |
  | GONV | `Matt_id`, `Indelning_typ`, `Indelning_etikett`, `Kon`, `Alder`, `Matperiod_*`, `Varde`, `Kommentar` |
  | NRAA, LRHST, OB, NRTV | Native names: `MBusinessKey`, `GroupTypeName`, `GroupName`, `Gender`, `AgeLabel` or `Age`, `MeasurementPeriod`, `Value`, `Commentary` |
  | PS | `MBusinessKey`, `GroupName`, `Kön`, `Ålder`, `Mätperiod`, `Värde`, `Kommentar` |

  - *Deviation:* the design retried only dropped optional columns. Instead, one
    one-row probe per area (and per run) picks the first candidate column that
    the table has, for each logical column. The candidates are `TRENDER_COLUMNS`
    in `api.py`. This costs one to four requests per area.
  - The resolved names are stored per dataset in
    `extension.socialstyrelsen_analys.columns`, which retrieval uses.
  - An area that lacks a required logical column is reported in
    `areas_unsupported` while it has no accepted documents. Once it has
    documents, this is a metadata failure.
- **Catalogue columns also differ.** LRHST has no `MTypeName`; NPS has neither
  `Unit` nor most descriptive columns. Optional catalogue columns named in
  Superset's HTTP 400 `Columns missing in dataset: [...]` answer are dropped, and
  the query is retried once. A missing `MBusinessKey`, `HrefIndicatorDashboard` or
  `DataGenerationDate` puts the area out of scope, in the same way as above.
- **Indicators listed in several areas** become separate datasets. For example,
  PS also lists G013 and G022, giving `PS|G013` next to `GONV|G013`.

## Output

`data/dataanalys/metadata/<owner>__multilingual__<digest>.jsonl.gz` holds one Dataset per
catalogue row. The 2026-10-09 catalogues listed 104 indicators in the six supported
areas: GONV 25, PS 32, OB 19, LRHST 15, NRTV 10 and NRAA 3. NPS listed 5, but its
values table is unsupported.

- **Dimensions.** The order is `Region`, `Kon`, `Alder`, `Tid`, `ContentsCode`.
  `Kon` and `Alder` are left out when the area has no such column, or when the
  column is null for every row of the indicator.
  - `Region` lists Riket first, then the regions sorted by name. It is eliminable
    to `Riket`, and its namespace records each category's `level`.
  - `Kon` is eliminable to `Kön, totalt`, and `Alder` to `Ålder, totalt`, when
    that category exists. Other ages sort numerically.
  - `Tid` holds `Matperiod_char` codes ordered by `Matperiod_datum`. LRHST mixes
    annual and rolling three-year periods (`2010 - 2012`).
  - `ContentsCode` has the single category `Matt_id`, with the unit label from
    `Unit`.
- **Labels.** The root label and the measure label come from `MTypeName`, else
  `MName`, else the catalogue link text without its trailing `". {Unit}"`, else
  the code.
- **Dates and text.**
  - `updated` is the UTC date of `DataGenerationDate` (epoch milliseconds). The
    original value is kept as `updated_original`.
  - `source` is `DataSource` when it is not empty.
  - `description` is `Description`. *Deviation:* the design did not list it, but
    the field is the shared one for this text.
  - `note` holds `Önskat värde: …`, `Indikatortyp: …` and a note on what `status`
    means, which names the comment column.
- **Shared fields.**
  - `doc_url` is `LinkIndicatorDescription`, when it is an absolute http(s) URL.
  - `data_url` is `https://dataanalys.socialstyrelsen.se/api/v1/chart/data`.
  - `metadata_url` is `…/api/v1/dashboard/{id}/datasets` of the resolved dashboard.
  - `time_unit` is `annual` for `1_ar`, `other` for any other period type, and
    omitted when the area has no period-type column (LRHST).
  - `first_period` and `last_period` are the first and last `Tid` categories.
  - `subject` is `{code: AREA, label: AreaName}`. When `AreaName` is missing, the
    label is the area start page's title, or else the area code.
- **`extension.socialstyrelsen_analys`.**
  - `area`, `indicator`, `values_dataset_id`, `values_dataset`,
    `catalogue_dataset_id` and `catalogue_dataset`.
  - `columns`: the map retrieval needs.
  - `dashboard_id`, `dashboard_slug` and `link_resolution`.
  - `measure_period_type` and `updated_original`.
  - `source_record`: the catalogue row, verbatim.
  - Dashboard `changed_on_utc` and chart ids are deliberately not stored, because
    they change with layout edits.

### Web links (`source_url`)

Every indicator should link back to its human-readable dashboard,
`https://dataanalys.socialstyrelsen.se/superset/dashboard/{slug}/`. The dashboard is
chosen in this order:

1. **`href`.** The slug parsed from the catalogue's `HrefIndicatorDashboard` HTML
   snippet, when it is a listed, published dashboard.
2. **`chart_filter`.** Otherwise, a published dashboard holding charts whose
   `form_data.adhoc_filters` filter `Matt_id IN [...]` on the code. The chart
   list's `dashboards[].id` is used, not `form_data.dashboards`, which is stale.
   Dashboards tagged `OMR: {AREA}` are preferred, then the most specific one (the
   fewest filtered codes), then the lowest id.
   - *Deviation:* the design used only the tag and the lowest id. That rule picks
     the regional profile dashboard (id 115), which filters on every GONV
     indicator, over the indicator's own dashboard.
3. **`area_overview`.** Otherwise, the area start page (tags `OMR: {AREA}` and
   `Start`). Every NRAA catalogue link points to a dashboard that does not exist,
   so NRAA links to `adhd_autism`.
4. **Unresolved.** Otherwise, `source_url`, `metadata_url` and the dashboard
   fields are omitted, and the key is listed in `links_unresolved`.

Further rules:

- Chart URLs (`/explore/?slice_id=`) are never used, because they redirect to
  login.
- Dashboard pages are never probed.
- If the dashboard listing is incomplete, only rule 1 is trusted. A fallback is
  then a metadata failure that keeps the last document.

`state/current.jsonl.gz` holds each dataset's `verified_at`, `checked_at`,
publication status and error, plus the withheld documents. `reports/latest.json`
also lists `areas`, `areas_incomplete`, `areas_unsupported`,
`unsupported_datasources`, `links_unresolved`, `out_of_scope` and the request
count.

## Weekly refresh

The workflow runs every Monday at 06:45 Europe/Stockholm. The site has no change
signal that covers the dimension categories, so **every run rebuilds every
indicator**. An identical rebuild keeps the accepted bytes and is counted as
`unchanged`.

A full run makes about 140 requests, one at a time, at least
`request_interval_seconds` (1.0) apart. That takes 3 to 5 minutes:

- 5 chart-list pages and 1 dashboard-list page.
- About 9 catalogue queries.
- About 20 column probes.
- One dimension query per indicator.

Listing details:

- *Deviation:* Superset rejects `order_column:id` ("Invalid order by column"), so
  the lists use the default order. Each list is complete only when the rows
  received match `count` and every id is unique.
- Every `/chart/data` query sends `force: false`, so Superset may answer from its
  86,400-second cache.

```powershell
uv run --extra harvest python -m harvesting.runner run --adapter dataanalys
uv run --extra harvest python -m harvesting.runner collect --adapter dataanalys
uv run --extra harvest python -m harvesting.runner run --adapter dataanalys --subject GONV NRAA
uv run --extra harvest python -m harvesting.runner run --adapter dataanalys --indicator G082 PS|G013
```

- `--subject` selects area codes (`run(areas=...)`).
- `--indicator` selects `MBusinessKey` codes or dataset codes
  (`run(indicators=...)`). Every selected area's catalogue is still read.
- Either option makes the run bounded: it removes nothing.
- `--full` is rejected, because every run is already a full rebuild.

### Failures and publication

- **Listings.**
  - If a chart-list or dashboard-list page fails or is not a Superset list, the
    failed discovery scope stops paging and records a structured error. Previously
    understood pages and the other listing still run; absence cannot remove an
    accepted dataset. A change in the reported count between pages also makes the
    scope incomplete.
  - A short or duplicated listing is a `discovery` error for `charts` or
    `dashboards`. The run continues, but removes nothing. These two resources mark
    the whole scope as failed.
  - Malformed chart or dashboard rows are reported individually
    (`charts/{id}`, `dashboards/{id}`) and stop removal.
  - Conflicting duplicate chart or dashboard ids are quarantined; neither listing
    row is selected as an authoritative replacement.
- **Area endpoint.** If an area's catalogue or probe is unreachable, that is an
  `endpoint` error for `*/{AREA}`. It is reported once, the area's documents are
  withheld, and the area is retried once after `retry_cooldown_seconds`. Any
  other catalogue or probe failure is a `metadata` error for `*/{AREA}`, which
  keeps the documents. A truncated catalogue (`row_limit` 1,000 reached) counts as
  such a failure.
- **Catalogue rows.**
  - A malformed row is reported as `discovery` `catalogue/{AREA}/{position}`.
    Later rows are still processed, and no removal happens in that area.
  - A duplicated `MBusinessKey` is quarantined as a `metadata` error and keeps its
    last accepted document.
- **Indicator endpoint.** An indicator's dimension query is its endpoint check.
  `Unreachable` is an `endpoint` error, and the document is withheld. Any other
  failure is a `metadata` error that keeps the last document. A truncated cube
  (20,000 rows reached) counts as such a failure.
- **Publication rule.** The shared `harvesting.currency.settle` publishes a
  dataset only while its endpoint works and its document was verified within 60
  days (`max_age_days`). Otherwise the document is withheld in state and restored
  automatically.
- **Removal.** Only an unbounded run with a complete, fully understood chart list
  removes datasets. It removes:
  - areas no longer listed;
  - indicators that a complete catalogue no longer lists;
  - indicators that are now out of scope.

## Retrieval

The `socialstyrelsen_analys` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/id", "/extension/socialstyrelsen_analys")`.

```python
from retrieval.adapters.dataanalys import retrieve

fragment = await retrieve(
    session,
    provider_code="socialstyrelsen_analys",
    dataset_code=document["extension"]["nordicintel"]["dataset_code"],
    language="sv",
    selection=selection,
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"].get("metadata_url"),
    metadata={
        "/id": document["id"],
        "/extension/socialstyrelsen_analys": document["extension"][
            "socialstyrelsen_analys"
        ],
    },
)
```

- **Request.** Each request is one `POST /api/v1/chart/data` on
  `values_dataset_id`, with `metrics: []`.
  - The columns come from the harvested map, plus the value and comment columns.
  - The filters are the selection: `Matt_id`, the Riket and Region levels, region
    names, sex, age and period.
  - `row_limit` is the number of cells plus one. More rows than cells means a cell
    has two values, which raises `RuntimeError`.
- **Splitting.** A selection above 10,000 cells is split by halving the longer of
  Region and Tid. At most four parts are fetched at once. Nothing is retried.
- **Values.**
  - A number is the value; a non-empty comment is added verbatim to `status`.
  - A null value with a comment becomes `null` plus the comment in `status`
    (for example "Uppgift saknas (via vården i Siffror)").
  - A null value without a comment is left out.
  - `value` is a dense list only when every cell is present; otherwise it is a
    sparse object.
- **Errors.**
  - `ValueError` for a selection that misses a dimension, or for Superset's HTTP
    400 `Columns missing`.
  - `LookupError` for HTTP 404 (an unknown datasource answers
    `{"message": "Not found"}`). A query with `status: failed` raises
    `RuntimeError`: a timed-out query does not prove the dataset disappeared.
  - Everything else propagates, including HTTP 401 or 403 if public access is
    closed.

## Verification

```powershell
uv run --extra harvest pytest tests/dataanalys
uv run --extra harvest python scripts/check_dataanalys_live.py
```

- **Tests.** The tests use answers recorded on 2026-10-09 for GONV (G082, G013,
  G033, G200), NRAA (NRAA_1, NRAA_2) and LRHST (LRHST09). These include the real
  HTTP 400 answers for LRHST's missing `MTypeName` and for the native column
  names.
  - The recordings are trimmed, as described in `tests/dataanalys/conftest.py`.
  - Labelled synthetic additions cover a malformed row, a duplicate key, a broken
    link and an unpublished dashboard.
  - Retrieval runs against an in-process Superset imitation built from G082's real
    rows.
- **Live check.** The live script makes about 12 requests. It reads both lists
  and the GONV catalogue and probe, rebuilds and validates G082 and G013, and
  retrieves one Riket total for the newest period of each. It writes
  `output/dataanalys-live.json` without observations.
- **Other live runs on 2026-10-09.** Bounded live runs also built and validated
  NRAA_1–3, LRHST09, OB11, PS390-A and NRTV T1, and found NPS unsupported.

- **Fresh completion verification, 2026-10-09.** The bounded live script passed
  again after discovery/retrieval fixes: 492 chart rows and 96 dashboard rows,
  both complete; GONV G082 and G013 metadata validated and each returned one
  schema-validated Riket total for 2025. Twelve requests were made, and no
  observations were saved. The report is `output/dataanalys-live.json` and carries
  its UTC `checked_at` timestamp. This is a bounded compatibility check, not full
  corpus acceptance or a hosted publication run.
- **Regression coverage.** All 69 adapter fixture tests passed; Ruff checks and
  formatting passed. Fixture tests additionally prove partial-page discovery
  isolation, retention after incomplete discovery or a bounded out-of-scope
  result, malformed chart-filter isolation, conflicting listing-id quarantine,
  cancellation propagation, and the
  distinction between a failed query and HTTP 404. Full automated acceptance,
  hosted monitoring/read-back and live whole-corpus byte-stability remain unverified.
  Failure telemetry is also tested through the real `Progress` transport: an
  endpoint error with its URL is visible while the next sibling is processed;
  a successful retry clears the final failure count/report without erasing the
  attempt event.

## Licence and rate limits

- No licence or terms of use are stated on the site or in the API.
- No rate-limit headers were seen.
- The adapter keeps to one request at a time, at least one second apart. It honours
  `Retry-After` and retries 429 and 5xx answers with backoff.
- Socialstyrelsen's indicator descriptions are linked as `doc_url`.

## Open risks

- **Undocumented API.** There is no OpenAPI specification. The custom queries work
  only because Superset's public role may read these tables. If the operator
  tightens the public role, the harvest and retrieval both fail: every area
  becomes a red `*/AREA` error, and retrieval propagates HTTP 401 or 403.
- **Untested `row_limit` cap.** Superset's own row cap is untested. The largest
  dimension answer seen had 1,320 rows. Retrieval splits at 10,000 cells, and the
  harvest flags 20,000 rows as truncated.
- **Partly verified column sets.** Column names were verified for one or more
  indicators in each area, not for every indicator. A renamed column becomes a
  `*/AREA` metadata failure that keeps the documents.
- **Region names, not codes.** Regions are names, not SCB codes, and there is no
  mapping table.
- **Overlap.** Datasets overlap with the [SDB adapter](sdb.md)
  (`socialstyrelsen`) and with Vården i siffror (SKR). Some values carry "via
  vården i Siffror" in their comment. Cross-area indicators such as G013 appear
  in both GONV and PS.
- **No licence statement.** See above.
- **HTML parsing.** Catalogue links are parsed from an HTML snippet
  (`HrefIndicatorDashboard`). Area start pages depend on the `OMR: {AREA}` and
  `Start` dashboard tags.
- **Unstable list order.** The default list order might shift while paging; the
  count and id checks detect this.
- **Failure telemetry.** Area and indicator attempt errors are emitted immediately
  with the request URL, before processing their siblings. Permanent report errors
  are settled after retries; a recovered attempt stays visible in telemetry and
  does not count as a final failure. Hosted monitor read-back remains unverified.

## Full local acceptance (9 October 2026)

The complete configured metadata harvest accepted **94 datasets** with zero
unresolved failures. Every accepted document passed the shared metadata schema
and contains `value: []`; observations were not saved. Ten indicators remain
explicitly outside the supported scope. See the
[acceptance evidence](../verification/new-adapters-acceptance.json) for coverage,
repeat-run scope and collection hashes. This supersedes the earlier sample-only
coverage limits; hosted execution remains unverified because GitHub Actions
refused to start jobs due to the account billing/spending limit.

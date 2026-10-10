# Riksbank

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

The `metadata_harvest.adapters.riksbank` package implements metadata and on-demand
retrieval for three Sveriges Riksbank APIs at `https://api.riksbank.se`:

- **SWEA** (`/swea/v1`): interest rates and exchange rates, 117 series in 17 groups;
- **Monetary Policy Data** (`/monetary_policy_data/v1`): forecast vintages of 24
  series, one per policy round;
- **SWESTR** (`/swestr/v1`): the Swedish krona Short Term Rate.

The provider code is `riksbank`. Access is anonymous. Research, samples and the
operation lists are in [provider documentation](https://developer.riksbank.se/).

## Scope decisions

- **English only.** [catalogs.json](../../catalogs.json)
  configures `en`. The APIs have no Swedish texts (`/swea/v1/Groups/en` is HTTP 400
  and `Accept-Language` has no effect), so no Swedish documents are produced.
- **Three APIs, about 42 datasets.** Holdings (`asset-management-api`, parquet)
  and Turnover Statistics (`selma-api`) are listed in the developer portal but are
  out of scope. SWEA cross rates (group 131) and monthly/annual aggregates (groups
  133, 134) have no series of their own and are not datasets.
- **SWESTR fixing only.** The compounded averages (`/swestr/v1/avg/…`, e.g.
  `SWESTRAVG1W`) and the index (`/swestr/v1/index/…`, `SWESTRINDEX`) answered on
  2026-10-09 but are not harvested; they have no catalogue and other fields.

## Output

`data/riksbank/metadata/<owner>__multilingual__<digest>.jsonl.gz` holds one Dataset per unit:

| Unit | `dataset_code` | Dimensions | Probe (endpoint check) |
| --- | --- | --- | --- |
| SWEA group with series | `swea_group_{groupId}`, e.g. `swea_group_2` | `Series` (metric) × `Time` (daily) | `GET /swea/v1/Observations/Latest/ByGroup/{groupId}` |
| Forecast series | `mpd_{series_id}`, e.g. `mpd_SEQGDPNAYSA` | `Series` (metric, one category) × `PolicyRound` × `Time` | `GET /forecasts?series={id}&policy_round_name={latest round}` |
| SWESTR | `swestr` | `Measure` (metric) × `Time` (daily) | `GET /swestr/v1/latest/SWESTR` (also its discovery) |

- **SWEA groups.** Label, description and thematic `paths`/`subject` come from
  `/Groups` (the top group under the root is the subject). Series labels are
  `midDescription`, or `shortDescription` when the mid text is a long notice (some
  STIBOR series), in which case the notice becomes a note; `longDescription` is
  the category note. Per-series `observationMinDate`, `observationMaxDate`,
  `seriesClosed`, `shortDescription` and the probe's `latestObservationDate` are
  under `dimension.Series.extension.riksbank.categories`. The research report
  claimed every series is closed; the live data has 77 open and 40 closed series,
  and the documents follow the data. `discontinued` is true only when every series
  of the group is closed.
- **Units.** SWEA reports no units. `SWEA_UNITS` in `mapping.py` maps each group,
  with a comment per entry: per cent for rate and yield groups, `index, 18 November
  1992 = 100` for TCW and KIX, `SEK per unit of currency` for group 130 (verified:
  JPY 0.06297 and IDR 0.00056, so not per 100), `SEK per SDR` for group 138. Group
  155 (forward premiums) has no documented unit and gets none. Forecast units and
  decimals come from `series_ids`; SWESTR units from the Riksbank's SWESTR page
  (volume in MSEK).
- **Forecasts.** The label is `<description>, <unit> (<series_id>): forecasts by
  policy round`, because descriptions repeat (six series are "GDP").
  `PolicyRound` lists every round of `/forecasts/policy_rounds` (61, `2015:1` …
  `2026:3`); rounds without a vintage for the series (such as `2020:2`) have no
  data. `updated` is the latest round's `policy_round_end_dtm`.
- **Native fields.** `extension.riksbank` holds `api` and `group_id`,
  `series_id` or `interest_rate_id`, plus `source_record` (SWEA group and series
  rows, the forecast catalogue row), the latest vintage's metadata or the latest
  SWESTR publication flags.
- **URLs.** `source_url` is the riksbank.se page of the API, `doc_url` the
  developer portal page (`https://developer.api.riksbank.se/api-details#api=…`,
  a single-page app that resolves the fragment client-side), and
  `link.describedby` the riksbank.se "via API" page. `metadata_url` is
  `/swea/v1/Series/ByGroup/{id}`, `/forecasts/series_ids` or
  `/swestr/v1/latest/SWESTR`; `data_url` is `/swea/v1/Observations`,
  `/forecasts?series={id}` or `/swestr/v1/SWESTR`. All page URLs answered 200 on
  2026-10-09.

### Time representation (deviation from the research sketch)

Metadata must list complete dimensions, but the observation dates are only known
from observations. The cheapest correct option, with no extra request, is used:

- **SWEA:** every calendar date from the group's earliest `observationMinDate` to
  the latest of `observationMaxDate` and the probe's latest dates. This is a
  superset of the banking days that have observations; retrieval leaves the other
  days out. Group 130 starts 1900-01-02 (`SEKETT`), so its `Time` has about 46,300
  codes; the index is a plain array without labels. The research example listed
  only four observed days, which is not a complete dimension.
- **Forecasts:** every period-end date of the series' frequency (monthly,
  quarterly or annual, inferred from the spacing of the latest vintage's periods)
  from `start_date` to the last period of that vintage. The id's frequency letter
  is not reliable: `SEDMENAYNA` has `D` but quarterly periods. Older vintages end
  earlier, so this covers every round.
- **SWESTR:** every calendar date from 2021-09-01, the first fixing served by the
  API, to the latest fixing date.

Because `observationMaxDate` and the latest fixing move every banking day, SWEA and
SWESTR documents are rebuilt (`rebuilt`, not `unchanged`) on most weekly runs.

`state/current.jsonl.gz` holds each dataset's `verified_at`, `checked_at`,
publication status and error, plus withheld documents. `reports/latest.json` also
records the per-API discovery status (`apis`) and the request count.

## Weekly refresh

The workflow runs every Monday at 07:15 Europe/Stockholm. The APIs publish no
ETag, Last-Modified or metadata timestamp, so **every run rebuilds every dataset**;
an identical rebuild keeps the accepted bytes and counts as `unchanged`.
`--full` is rejected: every run is already a full rebuild.

**Request budget.** 5 discovery requests (`/Groups`, `/Series`, `series_ids`,
`policy_rounds`, latest SWESTR) and one probe per SWEA group (17) and forecast
series (24): 46 requests. The client waits `request_interval_seconds` (15 s)
between requests, so a full run takes about 12 minutes; one endpoint retry adds a
60 s cooldown plus the retried requests. The unfiltered `/forecasts` (28.8 MB) is
never requested.

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter riksbank
uv run --extra harvest python -m metadata_harvest.runner collect --adapter riksbank
uv run --extra harvest python -m metadata_harvest.runner run --adapter riksbank --subject swea_group_2 SEQGDPNAYSA swestr
```

`--subject` accepts dataset codes, SWEA group ids (`2`), SWEA series ids
(`SEKEURPMI` selects its group) and forecast series ids, case-insensitively. A
bounded run reads only the catalogues needed to resolve the names.

### Politeness and keys

Anonymous clients get HTTP 429 with `Retry-After` 45–56 s after about five quick
calls; the portal says `Observations/ByGroup` allows five calls per minute.
`RiksbankClient` paces one request per 15 s, honours `Retry-After` (up to 180 s),
retries 429, 5xx and timeouts up to four attempts, and then raises `Unreachable`.
If `RIKSBANK_SUBSCRIPTION_KEY` is set (the workflow passes an optional repository
secret of that name), its value is sent as `Ocp-Apim-Subscription-Key`; it is never
required and never logged.

### Failures and publication

- **Discovery scopes.** `swea` (`/Groups` + `/Series`), `mpd` (`series_ids` +
  `policy_rounds`) and `swestr` (latest fixing) are independent. A failed scope is
  reported as `{stage: "discovery", resource: "swea" | "mpd" | "swestr"}`; its
  datasets keep their documents (withheld if the API was unreachable, still
  published if it answered unparseable data) and the other APIs continue. Only
  when **every** attempted scope fails does the run raise and publish nothing.
- **Listing rows.** A malformed `/Series` or `series_ids` row, a duplicated series
  id or group id, or a series whose group is missing from `/Groups` is reported
  (resource `swea/series.<n>`, `mpd/series_ids.<n>` or `en/<dataset_code>`); the
  affected datasets are quarantined (not rebuilt, last document kept), later rows
  still build, and the listing is incomplete so it removes nothing.
- **Dataset endpoint.** A dataset's probe is its endpoint check; an unreachable
  probe is retried once after `retry_cooldown_seconds`.
- **Metadata failures.** A probe that answers but cannot be mapped (for example no
  vintage in the latest round) keeps the previous document.
- **Publication rule.** Shared with UKÄ, SDB and Trafikanalys in
  `metadata_harvest.currency`: a dataset is published only while its endpoint
  works and its document was verified within 60 days.
- **Removal.** Only an unbounded run with a complete listing removes a group or
  forecast series that the catalogue no longer lists.

## Retrieval

The `riksbank` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/id", "/extension/riksbank")`.

```python
from metadata_harvest.adapters.riksbank.retrieval import retrieve

shared = document["extension"]["nordicintel"]
fragment = await retrieve(
    session,
    provider_code="riksbank",
    dataset_code=shared["dataset_code"],
    language="en",
    selection={"Series": ["SECBREPOEFF"], "Time": ["2026-10-05", "2026-10-06"]},
    data_url=shared["data_url"],
    metadata_url=shared["metadata_url"],
    metadata={
        "/id": document["id"],
        "/extension/riksbank": document["extension"]["riksbank"],
    },
)
```

- **Requests.** SWEA: `GET /swea/v1/Observations/{seriesId}/{from}/{to}` per
  selected series, covering the selected dates. Per-series calls have no
  documented span limit; 1900-01-01 to 2026-10-09 (32,211 rows, 1 MB) answered in
  one call, so ranges are split only beyond `MAX_SPAN_DAYS` (46,400 days).
  `Observations/ByGroup` (one year per call, five calls per minute) is not used.
  Forecasts: `GET /forecasts?series=&policy_round_name=` per selected round.
  SWESTR: `GET /swestr/v1/SWESTR?fromDate=&toDate=` per range.
- **Pacing is the caller's job.** Requests are sequential through the caller's
  session; nothing sleeps or retries. A selection of many series or rounds makes
  as many requests, and the anonymous quota is about four per minute.
- **Values.** Numbers, as published. Days and periods without an observation are
  left out (sparse); there are no markers. SWESTR `status` is `republication`
  and/or `alternative calculation: <reason>` for flagged fixings (explained in the
  document's note).
- **Errors.** `ValueError` for a missing or unknown dimension, a non-date `Time`
  code, an unknown SWESTR measure or an HTTP 400. `LookupError` for a gone group,
  series or rate id: SWEA answers 204 for unknown series and groups, and the
  forecast API 404 for unknown series *and* for rounds without a vintage, so when a
  series returns nothing retrieval checks `metadata_url` once and raises only if
  the series is no longer listed. Other HTTP errors propagate.

## Verification

```powershell
uv run --extra harvest pytest tests/riksbank
uv run --extra harvest python scripts/check_riksbank_live.py
```

The tests use responses recorded from the research samples (see the
`tests/riksbank/conftest.py` docstring), a stand-in client for the lifecycle and
an in-process imitation of the three APIs for retrieval. The live script makes
ten paced requests: it builds and validates `swea_group_2`,
`mpd_SEQGDPNAYSA` and `swestr`, retrieves a recent selection of each, and writes
`output/riksbank-live.json` without observations.

On 2026-10-09, the anonymous bounded check passed for all three documents:
17 SWEA groups and 24 forecast series were discovered without listing problems;
the selected fragments contained 5 SWEA values, 7 forecast values and 10 SWESTR
values. Metadata and retrieval-fragment validation passed. The report counts
retrieval requests as well as discovery/probe requests, and the checker paces
each request, including any lookup needed to distinguish a missing dataset from
an empty selection. No observation cube or production collection was saved.

The fixture suite passed all 76 tests on 2026-10-09. Coverage includes malformed
forecast responses raising an upstream
failure rather than returning successful empty/partial data, and scoped discovery
failures resolving series aliases from the last accepted document before applying
withholding. A full live harvest, live incremental byte-stability check, hosted
monitoring acceptance and subscription-key retrieval remain unverified.

Focused operational fixtures also verify that discovery and dataset failures
appear in live telemetry with the request URL before the next sibling begins.
Recovered retry failures remain visible in telemetry while the final report
contains only unresolved failures.

## Licence and rate limits

No licence is stated. The Riksbank says the APIs are free of charge, that
exchange rates are indicative only and not for transactional purposes, and that
it may change the APIs; SWESTR has separate "Conditions for the use and
re-publication of SWESTR". Anonymous access is limited per IP address (see
[Politeness and keys](#politeness-and-keys)).

## Open risks

- **Quotas.** The anonymous quota is not published; 429 came after about five
  quick calls. The quota with a free portal key is unknown and untested here.
- **Units.** Group units are hand-made; forward premiums (group 155) have none. A
  new group gets no unit until `SWEA_UNITS` is extended.
- **Time sizes.** Group 130's `Time` has about 46,300 dates (validation takes about
  two seconds). Smaller daily `Time` dimensions would need observed dates, which
  cost one or more requests per group.
- **SWESTR ids.** Only `SWESTR`, `SWESTRAVG1W` and `SWESTRINDEX` were checked live;
  the other average ids are unverified, and averages/index are not harvested.
- **Span limits.** No per-series span limit was found; the API could add one.
- **Forecast vintages.** A series without a vintage in the latest round fails as a
  metadata error (document kept). `start_date` is assumed to cover older vintages.
- **Portal.** The developer portal's OpenAPI export has empty `paths`; the
  `api-version=2022-04-01-preview` data API is internal and may change.
- **Out of scope.** Holdings and Turnover Statistics APIs, SWEA aggregates and
  cross rates.

## Full local acceptance (9 October 2026)

The complete configured metadata harvest accepted **42 datasets** with zero
unresolved failures. Every accepted document passed the shared metadata schema
and contains `value: []`; observations were not saved. See the
[acceptance evidence](../verification/new-adapters-acceptance.json) for coverage,
repeat-run scope and collection hashes. This supersedes the earlier sample-only
coverage limits; hosted execution remains unverified because GitHub Actions
refused to start jobs due to the account billing/spending limit.

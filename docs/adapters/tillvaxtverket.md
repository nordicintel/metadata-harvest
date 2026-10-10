# Tillväxtverket

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

The `metadata_harvest.adapters.tillvaxtverket` package implements metadata and
on-demand retrieval for Tillväxtverket's open accommodation statistics
(inkvarteringsstatistik), served by a Dimensional Insight DivePort (Diver/Spectre)
query server at `https://oppnadata.tillvaxtverket.se`. The provider code is
`tillvaxtverket`. No authentication is needed.

Research: [provider documentation](https://tillvaxtverket.se/tillvaxtverket/statistikochanalys.675.html) (2026-10-09).
The server's own API description is `https://oppnadata.tillvaxtverket.se/api.yaml`
(DivePort Web API, OpenAPI 3).

## Scope decisions

- **Hard-coded registry.** The server has no listing endpoint (`/api/api/query`
  answers 404, `/api/api/query/inkvartering` 400). The six datasets of area
  `inkvartering` are a registry in `source.py`, each with its columns by role:
  `GuestNights_Capacity_Day`, `GuestNights_Capacity_Year`,
  `GuestNights_Capacity_Revenue_Month`, `GuestNights_Capacity_Revenue_Year`,
  `GuestNights_Country_Month` and `GuestNights_Country_Year`. **Adding or removing a
  dataset is a code change.** A dataset whose live columns differ from its entry
  fails as a metadata error until the registry is updated.
- **Swedish only.** Category values and labels are Swedish. data.europa.eu's English
  title and description (machine translated, `translation_meta`) are kept as
  `extension.tillvaxtverket.english_title`/`english_description`, never as an `en`
  document. `--language en` is rejected.
- **Titles, descriptions, contact.** Only data.europa.eu has them (record
  `https-oppnadata-tillvaxtverket-se-dataset_{lowercase name}`, catalogue
  `oppnadata`, harvested from Sveriges dataportal).
- **Column labels (deviation).** The brief assumed no labels exist. Every DCAT
  distribution links a label file,
  `https://oppnadata.tillvaxtverket.se/data/inkvartering/{name}_variabeletiketter.csv`
  (`Name;Type;Length;Label`, Windows-1252), for example `LOGIINTAKT` "Logiintäkt"
  and `ANKOMSTER_SVE` "Ankomster från Sverige". The adapter uses those labels for
  measures and dimensions and falls back to column names when the file fails.
- **Change signal (deviation).** The brief proposed one `limit=1` row. Rows of one
  file carry different `LOAD_DATE`s (national, county and municipality rows are
  loaded minutes apart, and `GuestNights_Country_*` mixes 2024/2025 and 2026
  loads), so one row is not a complete signal. The adapter uses two small
  requests: `?format=json&limit=0` (columns and `rowCount`) and
  `?dimension=LOAD_DATE&format=json` (every distinct load time).

## Output

`data/tillvaxtverket/metadata/<owner>__multilingual__<digest>.jsonl.gz` holds six datasets.

- **Identity.** `dataset_code` is `{area}/{name}`, for example
  `inkvartering/GuestNights_Country_Year`.
- **Region** (geo role) is built from `NIVA_NAMN`, `LAN_KOD` and `KOMMUN_KOD`:
  - `riket` for national rows. Their county and municipality columns hold only a
    filler (`Sekretesskyddad` in the revenue and country files, `Riket` in the
    capacity files). The filler is not a category.
  - `lan:<LAN_KOD>` for counties, labelled with `LAN_NAMN`.
  - `lan:Sekretesskyddad` preserves the daily-capacity source's county-level
    category whose county code and name are `Sekretesskyddad`. This is an
    unidentified suppressed category, with no inferred county code or coordinates;
    retrieval filters `NIVA_NAMN=Län` and `LAN_KOD=Sekretesskyddad` exactly.
  - `kommun:<KOMMUN_KOD>` for municipalities, labelled with `KOMMUN_NAMN`.
  - `kommun:<LAN_KOD>:Sekretesskyddad` for a county's municipality rows whose code
    is `Sekretesskyddad` (merged suppressed municipalities; label
    `Sekretesskyddad`). These rows carry real values.
  - `child` links: `riket` to the counties, each county to its municipalities.
    On 2026-10-09 the lists have 290 to 302 categories.
- **Anlaggningstyp** comes from `ANLAGGNINGSTYP_NAMN`: `Camping`, `Hotell`,
  `Sekretesskyddad`, `SoL`, `Stugbyar`, `Vandrarhem`. `Sekretesskyddad` is kept as a
  category, because these are real merged rows with values. There is no total.
- **Dagtyp** comes from `DAGTYP_NAMN` (`Helg`, `Vardag`), only in
  `GuestNights_Capacity_Year`. In the day file the column is derived from the date,
  so it is an attribute there.
- **Land** comes from `LAND_KOD` (country files), labelled with `LAND_NAMN`. Its
  four groupings (`LANDGRP_*`) are kept per category under
  `dimension.Land.extension.tillvaxtverket.categories`. They are not
  categories, because no rows exist per group.
- **Tid** (time role) comes from `AR` (annual), `PERIOD` (`2024-07`, monthly) or
  `FULL_DATUM` (`2024-07-01`, daily), with `first_period`/`last_period`. Derived
  columns such as month names and week days are attributes, recorded in
  `extension.tillvaxtverket.columns`.
- **ContentsCode** (metric role) has one category per measure column, labelled
  from the label file. No units are emitted: Tillväxtverket documents none, and the
  unit of `LOGIINTAKT` is unknown. A dimension note says so.
- **Elimination.** No dimension is eliminable (`elimination: false`); there are no
  total categories.
- **Dates.** `updated` is the date of the latest `LOAD_DATE`. Its raw text
  (`2026-09-28, 20:20:39`) is in `extension.tillvaxtverket.updated_original`.
- **Shared fields.** `source` is the DCAT publisher (`Tillväxtverket`).
  `description` is the DCAT Swedish description.
- **`official_statistics: true`** is set only when that description says
  "Sveriges officiella statistik". All six records say so on 2026-10-09: "Myndigheten
  Tillväxtverket är ansvarig för inkvarteringsstatistiken som är en del av Sveriges
  officiella statistik."
- **Contacts.** `contacts` holds the DCAT contact name with the publisher as
  organization. The DCAT e-mail `fornamn.efternamn@tillvaxtverket.se` is a
  placeholder: it stays raw in `extension.tillvaxtverket.dcat.contact_point` and is
  never promoted to a contact.
- **Notes.** The distributions' statistics statements ("Statistiken är preliminär
  under pågående år." and "I april publiceras föregående års slutgiltiga
  statistik.") are root notes. They are followed by adapter notes on `status`
  markers and on zeros.
- **URLs.**
  - `data_url`: `…/api/api/query/inkvartering/data/{name}.cbase?format=json`, the
    DCAT JSON distribution.
  - `metadata_url`: the same URL with `&limit=0`.
  - `source_url`: the data.europa.eu dataset page,
    `https://data.europa.eu/data/datasets/<id>`.
  - `doc_url`: the record's Tillväxtverket page, "Om inkvarteringsstatistiken"
    (`https://tillvaxtverket.se/…/ominkvarteringsstatistiken.4505.html`).
  - Deviation: the brief suggested a Tillväxtverket page as `source_url` when one
    is given. That page describes the whole statistic, not the dataset, so it is
    documentation (`doc_url`).
  - `link.describedby` lists the label file and `api.yaml`.
- **Native fields.** `extension.tillvaxtverket` holds `area`, `name`,
  `region_dimension`, `measure_dimension` and `dimension_columns` (the column map
  retrieval needs). It also holds `columns` (name, source type, role, label),
  `labels_available`, `updated_original`, the trimmed DCAT record `dcat`, and
  `source_record`, the change signal: columns, `row_count` and `load_dates`.

`state/current.jsonl.gz` holds each dataset's `verified_at`, `checked_at`,
publication status, error and a `rebuild` flag. The flag is set when the label file
failed, and forces a rebuild on the next run. Withheld documents are kept there too.
`reports/latest.json` holds counts, errors (with URLs), the quarantined datasets and
the request count.

## Weekly refresh

The workflow runs every Monday at 07:45 Europe/Stockholm, in mode `full`: every run
checks every dataset.

- **Unchanged signal.** When the signal (columns, `rowCount`, distinct `LOAD_DATE`s)
  and the mapping version are unchanged, the accepted cube is reused without
  distinct-value queries. The data.europa.eu record is still fetched and re-applied:
  identical output counts as `unchanged`, changed text as `relabelled`.
- **Changed signal.** Otherwise the cube is rebuilt with one distinct query per
  dimension: region (five columns in one request), accommodation type, day type,
  country (with its group columns), and time. The label file is fetched too.
- **`--full`** is rejected: there is nothing more to do than every run already does.

**Request budget** (one request per second, sequential):

| Run | Requests | Measured |
| --- | --- | --- |
| Unchanged | 3 per dataset, 18 in all | 6 for two datasets on 2026-10-09 |
| Full rebuild | 7 or 8 per dataset, about 45 in all | 15 for two datasets on 2026-10-09 |

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter tillvaxtverket
uv run --extra harvest python -m metadata_harvest.runner collect --adapter tillvaxtverket
uv run --extra harvest python -m metadata_harvest.runner run --adapter tillvaxtverket --product GuestNights_Country_Year
```

`--product` takes dataset names or codes, case-insensitively. Unknown names are
reported as `discovery` errors containing "Unknown".

### Failures and publication

- **No scope-level failure exists.** There is no listing, so each dataset is its own
  scope. A run goes red only on an exception: invalid configuration, an unexpected
  file in the published data, or a programming error. Such a run publishes nothing.
- **Endpoint.** The two signal requests are the dataset's endpoint check. A
  transport, 429 or 5xx failure after the client's retries (`Retry-After` is
  honoured) is an `endpoint` error. The dataset is retried once after
  `retry_cooldown_seconds`.
- **Metadata.** Changed columns, malformed responses or a failed validation are a
  `metadata` error. The endpoint answered, so the last accepted document stays
  published until it is older than `max_age_days`.
- **Quarantine.** One category code arriving with two labels (a conflicting
  duplicate) is a metadata error that quarantines the dataset. Its last accepted
  document is kept, and the dataset is listed under `quarantined`.
- **Dependencies.** data.europa.eu and the label file are dependencies. Their
  failure is reported as `dependency` (`<key>#data.europa.eu`, `<key>#labels`) and
  keeps the last accepted record or labels. A dataset that has never had one falls
  back to its name or its column names. The table itself never fails.
- **Publication rule.** It is shared through `metadata_harvest.currency.settle`: a
  dataset is published only while its endpoint works and its document was verified
  within 60 days. Otherwise it is withheld in state and restored automatically.
- **Removal.** A failure never removes anything. An unbounded run drops documents
  whose dataset is no longer in the registry, so removal is a code change; a bounded
  `--product` run never removes.

## Retrieval

The `tillvaxtverket` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/id", "/extension/tillvaxtverket")` (the column map).

```python
from metadata_harvest.adapters.tillvaxtverket.retrieval import retrieve

fragment = await retrieve(
    session,
    provider_code="tillvaxtverket",
    dataset_code="inkvartering/GuestNights_Capacity_Revenue_Year",
    language="sv",
    selection={
        "Region": ["riket", "lan:01"],
        "Anlaggningstyp": ["Camping", "Hotell"],
        "Tid": ["2024", "2025"],
        "ContentsCode": ["ANTAL_GASTNATTER", "LOGIINTAKT"],
    },
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"]["metadata_url"],
    metadata={
        "/id": document["id"],
        "/extension/tillvaxtverket": document["extension"]["tillvaxtverket"],
    },
)
```

- **Filters.** Retrieval uses the documented simple filters
  `filter:<COLUMN>=<value>`: values of one column are OR-ed, different columns are
  AND-ed. The Spectre form (`filter=value("AR")=2024`) is not needed. One request
  group runs per region level (`filter:NIVA_NAMN=Riket|Län|Kommun` with `LAN_KOD` or
  `KOMMUN_KOD` values), with `column=` restricting the returned columns.
- **Selection.** Every dimension must be selected; there is no elimination.
- **Limits.** Pages hold 10,000 rows (DivePort's default; the documented maximum is
  500,000) and follow `rowCount`. URLs over 6,000 characters are split along their
  longest value list; a 6,680-character URL was accepted live. A selection needing
  more than 60 requests raises `ValueError`. That is checked before any request
  against the product of the selected category counts, and again after the first
  page against `rowCount`.
- **Values.** Values are numbers. The source declares every measure column
  `integer`, but JSON sometimes carries such values as floats (`1.6737801E7`).
  Integral floats in `integer` columns therefore become ints; other numbers are
  kept. Deviation: the brief said counts only, but the source types `LOGIINTAKT`
  `integer` as well.
- **Markers.** A non-numeric measure value, such as `Sekretesskyddad`, is `null`
  with the text in `status`. No such cell was seen live: suppression appears as
  merged rows.
- **No data.** Cells without a row or value are left out. `0` is kept.
- **Errors.**
  - `ValueError`: a missing dimension or measure, an unknown Region code, an
    unsplittable or over-cap selection, or DivePort's `Column "…" not found`.
  - `LookupError`: HTTP 404 or DivePort's `File not found`.
  - Other HTTP errors propagate as `aiohttp.ClientResponseError`.
  - Conflicting duplicate rows raise `RuntimeError`.
  - Missing, invalid or changing `rowCount`, or a page ending before that count,
    raises `RuntimeError`; retrieval never returns a partial fragment for an
    incomplete page sequence. The 60-request cap also counts actual short pages.
  - The caller's session is never closed, and nothing is retried.

## Verification

```powershell
uv run --extra harvest pytest tests/tillvaxtverket
uv run --extra harvest python scripts/check_tillvaxtverket_live.py
```

- **Tests.** They run offline. Each fixture entry names its provenance: samples
  under `docs/api/tillvaxtverket/samples/`, this adapter's bounded live probes of
  2026-10-09 (trimmed), and labelled reconstructed or synthetic bodies.
- **Live script.** It probes the signal of all six datasets, rebuilds and validates
  `GuestNights_Capacity_Revenue_Year`, and retrieves 16 cells from it: 19 harvest
  requests and two retrieval requests on 2026-10-09. The report labels the client
  counter `harvest_requests`, which excludes direct retrieval calls. It writes
  `output/tillvaxtverket-live.json` with counts only and fails an empty sample.

Fresh bounded verification on 2026-10-09 passed all six signal probes and the
Revenue Year rebuild/retrieval (16 cells). Fixture regressions additionally cover
incomplete pagination and short pages exceeding the actual request cap. Full
production acceptance, hosted monitoring, publication and a subsequent live
reuse/byte-stability run remain unverified; production data was not changed.

A metadata-only live distinct-region probe on 2026-10-09 additionally found 292
combinations in `GuestNights_Capacity_Day`, including one suppressed county-level
combination with all county/municipality code/name fields `Sekretesskyddad`.
The trimmed source row is recorded in the fixture `suppressed_county.json`;
regressions validate its metadata mapping and independent county/municipality
retrieval filters using explicitly synthetic measure values. No observation corpus
was downloaded. Mapping version 2 ensures this mapping is rebuilt on refresh.

## Licence and rate limits

The data are CC0 1.0: every DCAT distribution names
`http://creativecommons.org/publicdomain/zero/1.0/`. No rate limit is documented.
The adapter sends one request at a time, at most one per second, and honours
`Retry-After`.

## Open risks

- **Server-side aggregation.** `api.yaml` says `column=` with `dimension=`
  summarises with "the summary function used depends on the data source". That
  function is unverified, so no eliminable totals are offered.
- **Limits.** The maximum `limit` (500,000) is documented but untested. The URL
  length limit and any rate limit are unknown. The undocumented API may change on a
  DivePort upgrade.
- **Meaning of values.**
  - `SoL` is undocumented.
  - The unit of `LOGIINTAKT` is unknown (presumably kronor).
  - `0` also appears where a measure is probably not collected (for example
    `DISPONIBLA_RUM` for Camping); the source does not distinguish those cases.
  - Whether national rows are true totals of the county rows is unverified.
- **Other areas.** Other areas or `.cplan` files may exist on the server but cannot
  be discovered.
- **Coverage of this verification.** Four of the six datasets
  (`GuestNights_Capacity_Day`, `GuestNights_Capacity_Year`,
  `GuestNights_Capacity_Revenue_Month`, `GuestNights_Country_Month`) were
  live-probed but were rebuilt only from fixtures. Their first hosted run is their
  first full live rebuild.

## Full local acceptance (9 October 2026)

The complete configured metadata harvest accepted **6 datasets** with zero
unresolved failures. Every accepted document passed the shared metadata schema
and contains `value: []`; observations were not saved. The daily-capacity dataset
required a targeted recovery for the source-suppressed county category. See the
[acceptance evidence](../verification/new-adapters-acceptance.json) for coverage,
repeat-run scope and collection hashes. This supersedes the earlier sample-only
coverage limits; hosted execution remains unverified because GitHub Actions
refused to start jobs due to the account billing/spending limit.

# Försäkringskassan

The `metadata_harvest.adapters.forsakringskassan` package implements metadata and
on-demand retrieval for Försäkringskassan's statistics database
(*statistikdatabas*), the JSON API behind
<https://www.forsakringskassan.se/statistik-och-analys/statistikdatabas>:

- base `https://www.forsakringskassan.se/api/sprstatistikrapportera/public/v1`;
- no authentication. Each response sets a session cookie, which is not needed and
  is ignored;
- the provider code is `forsakringskassan`; research is in
  `docs/api/forsakringskassan/REPORT.md` (2026-10-09).

## Scope decisions

- **Discovery.** `GET /reports` is the catalogue: 22 benefit groups (`forman`) in 6
  areas, 97 reports. Each report's `GET /{report}/meta.json` lists every dimension
  with its complete category list, the measures (`filter.matt`) and one to five
  tables (`filter.uppdelning`). There is no OpenAPI description; the endpoints come
  from the web app.
- **One Dataset per table.** A report with several breakdowns has one table per
  breakdown. A dimension with a `group` belongs only to the table whose breakdown
  key equals that group; the others belong to every table. This rule was checked
  for all 97 reports (every `group` matches a breakdown) and against live rows of
  `sa-bestand-manad` and `tand-allmant`.
- **Swedish only.** The texts endpoint answers the same Swedish texts for
  `lang=en`, so no English documents are emitted (`languages: ["sv"]`; other
  languages are rejected).
- **Texts.** Report names, ingresses and descriptions live in a separate text
  service, `GET /rest-api/fk-rest-textnycklar/public/v2/meka?lang=sv-se` (860 KB,
  HTML escaped twice). It is fetched once per run.
- **Tables with undeclared dimensions are excluded.** 27 reports name a column in
  `table.order` that `meta.json` does not describe (`kommun_kod` in 21, also
  `diagnoskod3`, `diagnos_kod`, `diagnosavsnitt_kod`, `diagnos_egen`). Rows of
  some of their tables carry that column (for example
  `sa-bestand-manad/SAbestandmanadkommun` rows have `kommun_kod: "ALL_01"` and
  municipality codes), so those tables cannot be described completely. When a
  report has such a column, each of its tables is probed with one filtered
  request (`ALL` or the first category of each declared dimension). A table whose
  rows carry an undeclared key is not published; it is listed in the run report's
  `undeclared_dimensions` and counted as `excluded`, not as an error. Its siblings
  without the column are published. 65 tables are probed on a full rebuild.
- **Not used.** XLSX downloads (`{Table}.xlsx`, untested), chart hints
  (`diagram`, `pivot`), the UI's `selected` defaults (kept only inside the hashed
  `meta.json`).

## Output

`data/forsakringskassan/metadata/forsakringskassan__sv.jsonl.gz` holds one
metadata-only Dataset per table. `meta.json` lists 210 tables (205 distinct names;
five names are reused across reports, so the report stays in the code). Excluded
tables (above) reduce the published count; the first hosted run reports the exact
number.

- **Identity.** `dataset_code` is `{report}/{Table}`, for example
  `ase-beslut-lan/ASEBeslutLan`.
- **Label.** The report name from the texts (`MEKA.report.{report}.name`), plus
  `: {breakdown label}` for reports with several tables. `note` holds the ingress
  and an explanation of the `..` status. `description` is the `about` text as
  plain text.
- **Dimensions.** Dimension ids are the source keys, never renamed. Keys are not
  standardised across reports (`kon` vs `kon_kod`, `lan` vs `lan_kod1` …), so the
  alias table `ROLE_ALIASES` in `mapping.py` attaches shared roles and a concept
  (`extension.forsakringskassan.concept`):

  | Keys | Role | Concept |
  | --- | --- | --- |
  | `ar` | time | year (not when labelled as a birth year, "Barnets födelseår") |
  | `manad`, `man` | time | month |
  | `vecka` | time | week |
  | `tidsperiod` (a real dimension only in `tand-allmant`) | time | period |
  | `lan`, `lan_kod`, `lan_kod1`–`lan_kod4`, `lan_mottagning` | geo | county |
  | `kon`, `kon_kod`, `kon_kod1`–`kon_kod3` | — | sex |

  `row_nr` is a row counter, not a dimension. The `group` of a grouped dimension is
  kept in `extension.forsakringskassan.group`.
- **Totals.** `ALL` is the total category (`Kvinnor och män`, `Riket`, `Samtliga …`):
  such dimensions have `elimination: true` and `elimination_value: "ALL"`; others
  `elimination: false`.
- **Measures.** `filter.matt` forms the `matt` dimension with the metric role.
  A unit is set only when the measure's own label states one, verbatim: `Belopp i
  1000-tal kr` gives unit label `1000-tal kr`; `tusentals kronor`, `kr`, `kronor`
  and `%` likewise. Nothing is inferred from keys (`antal`, `belopp`), so a measure
  labelled only `Belopp` has no unit. The measure's paragraph in the `about` text's
  "Mått" section becomes its category note when the heading equals the label.
- **Shared fields.** `source` "Försäkringskassan"; `official_statistics` from
  `meta.official` (omitted for the four reports without it); `discontinued: true`
  for names marked "(uppdateras ej)"; `time_unit` from the finest time dimension;
  `first_period`/`last_period` only for year-only tables; `subject` = `forman`
  with its text heading; `paths` = area, then benefit; `contacts` from the
  `mailto:` links of the `about` text; `doc_url` = the quality declaration PDF when
  linked; all PDF links in `link.describedby`. Root `updated` is omitted: the
  source gives no date, and harvest time is not a source date.
- **URLs.** `source_url` `…/statistikdatabas#!/{area}/{report}`, `metadata_url`
  `{base}/{report}/meta.json`, `data_url` `{base}/{report}/{Table}.json`.
- **Native fields.** `extension.forsakringskassan` holds `report`, `table`,
  `breakdown`, `area`, `forman`, `measure_dimension`, `meta_version`,
  `meta_revision`, `computed` (verbatim, see risks) and `source_record`: the
  catalogue entry (`report`, `area`, `group`), a `meta.json` summary (key, area,
  forman, official, version, revision, tables) and the SHA-256 hashes of the
  `meta.json` and of the report's text entries.

`state/current.jsonl.gz` holds each report's accepted hashes, table list,
excluded tables and whether its last build was complete, plus each table's
`checked_at`, `verified_at`, publication status, error and withheld documents.

## Refresh mode and request budget

There is no `Last-Modified`, `ETag` or update field, and `meta.revision` is 0
everywhere. The change signal is a hash of the canonical `meta.json` (it changes
when a year or month is added) and of the report's text entries
(`MEKA.report.{report}`, `MEKA.forman.{forman}`, `MEKA.area.{area}`).

- **Incremental (weekly).** Every run reads `/reports`, the texts and every
  selected report's `meta.json` (99 requests). A report is rebuilt only when a
  hash or the mapping version changed, or its last build was incomplete; otherwise
  its tables are confirmed unchanged and count as verified.
- **Full (`--full`, monthly).** Every report is rebuilt, including the 65 probes:
  about 164 requests.
- **Pace.** One request at a time, at least one second apart; 429 and 5xx are
  retried with backoff, honouring `Retry-After`. A full run takes about three
  minutes.
- **Byte stability.** An identical rebuild keeps its accepted bytes and counts
  `unchanged`.

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter forsakringskassan
uv run --extra harvest python -m metadata_harvest.runner run --adapter forsakringskassan --full
uv run --extra harvest python -m metadata_harvest.runner run --adapter forsakringskassan --subject ase-avslutsanledning ase-beslut-lan
uv run --extra harvest python -m metadata_harvest.runner collect --adapter forsakringskassan
```

### Failures and publication

- **Catalogue.** If `/reports` fails or is not a list of groups, the run raises
  and nothing is published (whole-scope failure, resource `reports`). Malformed
  groups or report keys are reported as `discovery` errors
  (`reports/#<index>`, `reports/<group>#<index>`); valid later rows are still
  processed, but the listing is incomplete, so no report is removed.
- **Texts.** A failing or malformed text service is a dependency failure, reported
  once as `discovery`/`texts`. No document is rebuilt or verified in that run, and
  the last accepted documents stay; they become stale after `max_age_days`. No
  label-less replacement is ever published. A report missing from the texts is a
  metadata failure of its tables.
- **Report endpoint.** A failing `meta.json` is an `endpoint` failure of all its
  tables, reported once as `*/{report}`, and retried once after
  `retry_cooldown_seconds`. A malformed `meta.json` is a `metadata` failure
  `*/{report}`.
- **Tables.** A table whose document cannot be built or validated is a `metadata`
  failure `sv/{report}/{Table}` and keeps its last document. A failing probe is an
  `endpoint` failure of that table.
- **Duplicates.** A report listed twice in the catalogue, or a table listed twice
  in a report, is quarantined: reported, not rebuilt, previous documents kept.
- **Publication rule.** Shared with UKÄ, SDB and Trafikanalys
  (`metadata_harvest.currency.settle`, `max_age_days` 60): a table is published
  only while its report endpoint works and its document was verified within 60
  days; otherwise it is withheld in state and restored automatically.
- **Removal.** Only an unbounded run removes: reports absent from a complete
  `/reports` listing, and tables a parsed `meta.json` no longer lists.
- **Errors** carry `stage`, `resource`, `error` and the `url`; they also go to
  `Progress`. The job fails only for discovery errors on `reports` or `texts` and
  for `*/` endpoint failures.

## Retrieval

The `forsakringskassan` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/id", "/extension/forsakringskassan")`.

```python
from metadata_harvest.adapters.forsakringskassan.retrieval import retrieve

shared = document["extension"]["nordicintel"]
fragment = await retrieve(
    session,
    provider_code="forsakringskassan",
    dataset_code=shared["dataset_code"],
    language="sv",
    selection={
        "ar": ["2026"],
        "manad": ["01", "02"],
        "beslut_kod": ["ALL"],
        "lan_kod": ["01", "03"],
        "matt": ["antal"],
    },
    data_url=shared["data_url"],
    metadata_url=shared["metadata_url"],
    metadata={
        "/id": document["id"],
        "/extension/forsakringskassan": document["extension"]["forsakringskassan"],
    },
)
```

- **Request.** `GET {data_url}?dim=a,b&…`: every selected dimension except `matt`
  is a server-side filter, codes joined with commas (verified live; a repeated
  parameter counts only once, and `%2C` works too). Full tables can be several MB
  (`ASEBeslutLan` 3.6 MB), so a request never omits a filter. Query strings over
  1,800 characters are split along the longest code list; parts are fetched one
  at a time on the caller's session, never retried.
- **Selection.** Every dimension must be selected; eliminable dimensions select
  `ALL`. `matt` picks measures from each row.
- **Values.** Numbers (integral floats become integers). `rojd: true` with
  `value: null` is a suppressed cell: `null` with status `".."`, the marker the
  source's texts define ("När vi inte kan redovisa uppgifter markeras celler i
  tabeller med ".."."). `rojd: false` with `value: null` (not reported) and
  combinations without a row are left out.
- **Errors.** `ValueError` for selections without measures or a dimension, or too
  long to split. `LookupError` for HTTP 404 (unknown report) and for a table the
  report's `meta.json` no longer lists: an unknown table of a known report answers
  `[]`, so an empty result is checked against `meta.json` (one extra request).
  Rows with other dimensions than the selection, or conflicting duplicate rows,
  raise `RuntimeError`; transport and server errors propagate.

## Verification

```powershell
uv run --extra harvest pytest tests/forsakringskassan
uv run --extra harvest python scripts/check_forsakringskassan_live.py
```

The tests use the recorded 2026-10-09 samples (two `meta.json`, the texts excerpt,
the trimmed catalogue, two tables) plus labelled synthetic cases (key aliases,
an undeclared `kommun_kod` column, malformed and duplicate catalogue rows,
duplicate tables). The live script reads `/reports`, the texts and two
`meta.json`, validates their documents, and retrieves one filtered selection from
each report's first table (six requests); it writes
`output/forsakringskassan-live.json` without observations.

Fresh bounded verification on 2026-10-09 passed: the two reports produced three
validated documents, with 4 and 8 retrieved cells respectively. The report's
`harvest_requests` counts four metadata requests; the two retrieval requests are
additional. This verifies sampled compatibility only. Full production acceptance,
hosted monitoring, publication and a subsequent live reuse/byte-stability run
remain unverified; production data was not changed.

## Licence and rate limits

No licence or terms of use are stated by the API, the web page or the texts;
data.europa.eu lists the table URLs. No rate limits were observed (97 `meta.json`
requests at 1.2 s spacing); the adapter keeps one request per second.

## Open risks

- **Licence** unknown (see above).
- **Undocumented internal API** (`sprstatistikrapportera`, `fk-rest-textnycklar`)
  without versioning promise.
- **Undeclared dimensions.** Tables whose rows carry `kommun_kod` or diagnosis
  codes not described in `meta.json` are excluded (municipality breakdowns
  included). Their category lists would need another source.
- **Belopp units** are taken only where a label states them (`1000-tal kr`,
  `tusentals kronor`, `kr`); a few measures are labelled only `Belopp` and get no
  unit. Labels and the `about` text can disagree; the label is used.
- **`filter.computed`** is display-only as far as observed (`tidsperiod` =
  `${ar} ${manad}` in 52 reports; rows carry `ar` and `manad`). It is kept
  verbatim in `extension.forsakringskassan.computed`. `tidsperiod` is a real
  dimension only in `tand-allmant` (periods such as `juli 2024 - juni 2026`).
- **Table membership** relies on the `group` rule; it is confirmed by probes only
  for reports with undeclared columns.
- **Time role heuristic** by key; an `ar` labelled as a birth year is not time.
- **XLSX** untested; **no English** texts.
- **Probe inconclusive.** A probe that returns no rows is a metadata failure of
  that table (its previous document is kept).

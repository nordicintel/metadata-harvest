# SMHI Nationella emissionsdatabasen

The `metadata_harvest.adapters.smhi_emissions` package implements metadata and
on-demand retrieval for SMHI's national database of emissions to air,
`https://nationellaemissionsdatabasen.smhi.se/api/`.

- The API is the undocumented Django REST backend of SMHI's map and chart
  application. It has no authentication, no paging and no published rate limit.
  Everything here was read from the application and checked live on 2026-10-09.
- The provider code is `smhi`. Native fields live under `extension.smhi_emissions`,
  the adapter namespace, so a later adapter for another SMHI service cannot collide
  with them under the provider namespace.
- Swedish only. The database has no English labels.

The documents carry the attribution `source: "SMHI, Nationella emissionsdatabasen"`.
SMED produces the data for Naturvårdsverket.

## Scope decisions

- **One dataset per substance.** `getfilteroptions/` lists 29 substances (As … Zn).
  Each becomes one dataset with dimensions Region × Sektor × Tid × ContentsCode. A
  single cube with substance as a dimension would mix plain tonnes with CO2
  equivalents.
- **Regions come from a static SCB list.** No endpoint lists regions. The package
  ships `regions.py`: Sverige, 21 län and 290 kommuner, copied from SCB on
  2026-10-09.
- **The Sektor dimension is the full tree for every substance.** `getfilteroptions/`
  also says which substances each subsector reports. That list is kept as evidence
  in `listed_in_subsectors`, but it does not restrict the dimension. `getdata` returns
  non-zero values outside it. For example, CO2 has 150,659 t in 2024 under
  "Utrikes transporter", whose subsectors do not list CO2.
- **No Excel.** `getexcelfile` is not used for harvesting or retrieval. It would add
  a spreadsheet dependency and about 600 large downloads.
- **Only the current submission.** `submission` (the reporting round) is left out of
  every request. `submission=undefined` returns HTTP 500.

## Output

`data/smhi_emissions/metadata/smhi__sv.jsonl.gz` holds 29 metadata-only Dataset
documents, one per substance.

- **Identity.** `dataset_code` is `emissionsdatabasen_<substance code>`, for example
  `emissionsdatabasen_CO2`. The code is case-sensitive and never the numeric
  `substance_id`. The prefix leaves room for other SMHI datasets under the provider
  code `smhi`.
- **Label.** The label is constructed as `"<long_name>: utsläpp till luft efter
  region, sektor och år"`, for example "Koldioxid (CO2): utsläpp till luft efter
  region, sektor och år". `long_name` is SMHI's substance name, verbatim.
- **Region** has 312 categories. Codes are SCB codes with leading zeros (`00`, `01`,
  `0180`). Labels are the official names, with `00` labelled "Sverige". Children link
  `00` to the län and each län to its kommuner. Region is eliminable with
  elimination value `00`.
- **Sektor** has 62 categories:
  - `0` is "Totalt", with elimination value `0`.
  - The 9 main sectors use codes `1`–`9`.
  - The 52 subsectors use codes such as `4.1` and `7.12`.
  - Codes and labels come verbatim from `getfilteroptions`, typos included
    ("Slitage fran däck och bromsar").
  - Sectors `1` and `3` reuse their own code for their single subsector, so they
    have no children.
- **Tid** holds the years of the substance's national probe. In submission 2026
  these are 1990, 2000, 2005, 2010, 2015 and 2020–2024. `time_unit` is `annual`
  because each period is one year, and a note lists the years because the series
  has gaps. `first_period` and `last_period` are the first and last year.
- **ContentsCode** has one category, the substance code. Its label is the
  `long_name` and its unit is `ton/år`. The API response carries no unit; `ton/år`
  is the unit on every chart axis and Excel export of the database. CH4, N2O, HFC,
  PFC and SF6 are CO2 equivalents according to their names ("CO2-ekv."). A note
  says so; values are not altered.
- **Zeros.** A note states that the database reports combinations without
  emissions as 0. A zero can therefore mean no emissions or no applicable data, and
  there are no status markers.
- **Dates.** `updated` is omitted. The only change signal is `primary_submission`,
  the year of the reporting round (2026 now), not a date. It is kept as
  `extension.smhi_emissions.submission` and in `source_record`, and a note names it.
  The Excel report date (2026-05-12) is not read.
- **URLs and contact.**
  - `source_url` is the public application, `https://nationellaemissionsdatabasen.smhi.se/`.
  - `doc_url` is SMHI's information page,
    `https://www.smhi.se/data/luftkvalitet/nationella-emissionsdatabasen`.
  - `metadata_url` is `getfilteroptions/` and `data_url` is `getdata/`.
  - The contact is `emissionsdatabasen@smhi.se`, published on the information page.
- **Native fields.** `extension.smhi_emissions` holds:
  - `substance`: the code, `substance_id` and `long_name`.
  - `submission`.
  - `sectors`: the tree with labels, which retrieval needs.
  - `listed_in_subsectors`.
  - `request_parameters`: the region code rule.
  - `source_record`: the substance entry, the probe's substance key and the
    submission fields.

`state/current.jsonl.gz` holds each dataset's `verified_at`, `checked_at`,
`submission`, publication status and error, plus the withheld documents.
`reports/latest.json` also lists the submissions seen and whether the tree was
complete.

## Monthly refresh

The workflow runs on the 3rd of every month at 07:30 Europe/Stockholm. A new
submission arrives once a year, in spring, and revises the whole series back to 1990.
A full run is **30 requests**: one `getfilteroptions/` and one national `getdata`
probe per substance, paced at one request per second (about 30 seconds). **Every run
rebuilds every substance.** An identical rebuild keeps the accepted bytes and counts
as `unchanged`. A new submission changes the submission note and fields, and possibly
the years, so the documents are rebuilt. `--full` is not accepted, because every run
is already complete.

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter smhi_emissions
uv run --extra harvest python -m metadata_harvest.runner collect --adapter smhi_emissions
uv run --extra harvest python -m metadata_harvest.runner run --adapter smhi_emissions --subject CO2 NOx
```

`--subject` selects substance codes. An exact match is tried first, then a
case-insensitive one.

### Failures and publication

- **Discovery.** If `getfilteroptions/` fails or its sector tree is malformed, the
  run raises and publishes nothing. A malformed tree means a sector or subsector
  without code or label, colliding codes or labels, or a subsector outside its
  sector. This is the whole-scope failure; its resource is `filteroptions`.
- **Malformed substance entries.** A malformed entry is reported as a `discovery`
  error with resource `filteroptions/<subsector>/<position>`. Later entries are still
  read. The tree then counts as incomplete, so nothing is removed.
- **Conflicting duplicates.** A substance code listed with different `substance_id`
  or `long_name` values is quarantined. It is not probed, its last document is kept,
  and it is reported as a `metadata` error.
- **Endpoint failures.** A substance's national probe is its endpoint check. If the
  probe is unreachable, that is an `endpoint` failure. The probe is retried once,
  after `retry_cooldown_seconds`.
- **Metadata failures.** A probe that answers but cannot be mapped is a `metadata`
  failure, for example one with an unknown sector label, no years or non-numeric
  values. It keeps the last document.
- **Publication rule.** This uses the shared rule in `metadata_harvest.currency`. A
  dataset is published only while its probe works and its document was verified
  within 60 days. Otherwise it is withheld in state and restored automatically.
- **Removal.** Only an unbounded run with a complete tree removes a substance, and
  only when `getfilteroptions` no longer lists it.
- **Error records.** Every error records `stage`, `resource` (`sv/<dataset_code>`),
  `url` and `error`.

## Retrieval

The `smhi` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/id", "/extension/smhi_emissions")`.

```python
from metadata_harvest.adapters.smhi_emissions.retrieval import retrieve

fragment = await retrieve(
    session,
    provider_code="smhi",
    dataset_code="emissionsdatabasen_CO2",
    language="sv",
    selection={
        "Region": ["0180"],
        "Sektor": ["4.1"],
        "Tid": ["2024"],
        "ContentsCode": ["CO2"],
    },
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"]["metadata_url"],
    metadata={
        "/id": document["id"],
        "/extension/smhi_emissions": document["extension"]["smhi_emissions"],
    },
)
```

- **Requests.** Every request is `GET getdata/?county=&municipality=&hsektor=&usektor=0&sub=<code>`,
  and they are sent one at a time. For each selected region:
  - One request with `hsektor=0` serves `0` and the main sectors.
  - One request with `hsektor=<sector>` serves each main sector whose subsectors
    are selected.
  - Region codes lose their leading zeros. `00` becomes `county=0&municipality=0`,
    `01` becomes `county=1&municipality=0` (the län total), and `0180` becomes
    `county=1&municipality=180`.
- **Request cap.** At most **50 requests** per call (`MAX_REQUESTS`); a larger
  selection raises `ValueError`. Only regions split a selection. One region needs at
  most 8 requests; 50 regions fit for main sectors.
- **Label mapping.** Series are keyed by sector label. The harvested `sectors` map
  them back to codes; an exact match is tried first, then a case-insensitive one.
  Submission 2025 spelled "inrikes civil sjöfart" in lower case. In a sector's
  response, `Totalt` repeats the sector and is ignored. Main-sector values always
  come from `hsektor=0`, because the two responses differ in the last float digit.
  An unknown label raises `RuntimeError`, because the harvested tree is outdated.
- **Values.** Values are numbers exactly as SMHI returns them, `0.0` included. There
  is never a `status`. Years SMHI returns no point for are left out; for example,
  Kiruna's CH4 "Odling av mineraljordar" has no 1990 point. An unknown region
  returns no series, so `value` is `{}`.
- **Errors.**
  - `ValueError` for a missing dimension, another substance, an unknown sector or
    region code, or too many requests.
  - `LookupError` when `getdata` answers Django's HTTP 500 page and
    `getfilteroptions/` no longer lists the substance. A numeric or unknown `sub`
    also returns that page.
  - Other HTTP errors propagate as `aiohttp.ClientResponseError`, including an HTTP
    500 for a substance that is still listed.

## Verification

```powershell
uv run --extra harvest pytest tests/smhi_emissions
uv run --extra harvest python scripts/check_smhi_emissions_live.py
```

**Test fixtures.**

- The tests use the research samples `getfilteroptions.json`,
  `getdata-national-CO2.json` and `getdata-stockholm-NOx-transport-sub2025.json`.
- They also use six responses recorded on 2026-10-09:
  - Stockholms län CO2.
  - National CO2 Transporter.
  - Kiruna CH4 Jordbruk.
  - National Dioxin.
  - An unknown county.
  - The HTTP 500 page for an unknown substance.
- The other substances' probes are synthetic: the CO2 probe renamed.
- Further labelled synthetic cases cover a failing probe, an unknown sector label,
  malformed and conflicting rows, and a removed substance.

**Live check.** The live script makes 4 requests:

- `getfilteroptions/`.
- The CO2 and NOx probes, rebuilding and validating both documents.
- One retrieval: Stockholm kommun × Personbilar × the newest year.

It writes `output/smhi_emissions-live.json` without observations.

## Licence and rate limits

- Neither the application nor SMHI's information page states a licence or terms of
  reuse (checked 2026-10-09). It is not confirmed that SMHI's general open-data terms
  apply; ask `emissionsdatabasen@smhi.se` before redistribution.
- There is no documented rate limit. The harvester sends one request at a time, at
  least one second apart, and honours `Retry-After`.

## Open risks

- **Licence** to confirm with SMHI (above).
- **Undocumented API.** It can change with any redeploy of the application. Watch
  for these:
  - A failing `getfilteroptions/`, which makes the run red.
  - Unknown sector labels, which are metadata failures and a `RuntimeError` in
    retrieval.
- **Submissions.** Only 2025 and 2026 were seen to work, and no endpoint lists the
  available submissions. Only the current one is harvested.
- **Units of Dioxin and the metals.** Every value is labelled `ton/år`, as the
  application and the Excel exports do. National Dioxin is about 8·10⁻⁶ t (8 g) in
  2024, so the unit is plausible, but the method report may use g I-TEQ or kg. This
  is not verified.
- **Zeros.** Missing combinations come back as `0.0` and cannot be told apart from
  real zeros.
- **Region list.** The region list is static. A change to the kommun division needs
  an update to `regions.py`. Retrieval of an unknown code returns no data, not an
  error.
- **Län totals.** `county=<län>&municipality=0` returns the län total; this was
  verified for Stockholms län CO2 on 2026-10-09. Whether that total equals the sum
  of its kommuner was not checked.
- **Overlap.** The overlap with SCB/Naturvårdsverket PxWeb tables (MI0107) and with
  Kolada's emission KPIs was not checked. The kommun × subsector detail is probably
  unique to this database.

## Integration verification (9 October 2026)

The bounded live check passed for CO2 and NOx metadata and one Stockholm
observation selection (four requests, no observations saved). Regression coverage
also rejects non-finite source values instead of emitting invalid JSON numbers.
Full hosted coverage and publication are separate from this bounded check.

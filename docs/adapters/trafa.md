# Trafikanalys

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

The `harvesting.adapters.trafa` package implements metadata and on-demand
retrieval for the Trafikanalys open data API, `https://api.trafa.se/api`.

- It was ported from
  [trafa-adapter `936b20e`](https://github.com/nordicintel/trafa-adapter/tree/936b20eadd4ba9e748d6fb00c6ca67a7e414a290),
  keeping its harvest mapping. Its unfinished dataset-layout redesign was not ported.
- There is no runtime dependency on that repository.
- The provider code is `trafa`.

> **Temporary adapter.** Trafikanalys is absorbed into Tillväxtanalys at the end of
> 2026. This port is deliberately the simplest one. When the API is retired, the
> product-list request fails and every run goes red without publishing. At that
> point, disable `[adapters.trafa]` in [schedules.toml](../../schedules.toml) and
> remove the package, its workflow, its configuration and `data/trafa/`.

Cite the data as **"Källa: Trafikanalys"**. The documents carry this attribution as
`source`.

## Scope decisions

- **HTML discovery is retained, as a time-limited exception.** The valid API
  combinations of a product are listed only on its documentation page,
  `https://www.trafa.se/sidor/api-dokumentation/?code=<UniqueId>`. The harvest reads
  that page together with `/structure`. No observations are stored.
- **Swedish only.** [catalogs.json](../../catalogs.json) configures
  `sv`. Trafikanalys supplies no real English labels, and every English dataset was
  skipped as untranslated. The ported English path remains available with
  `--language en` after adding `en` to the configuration.
- **Exclusions.** The test product `ATFT` is excluded unless named with `--product`.

## Output

`data/trafa/metadata/<owner>__multilingual__<digest>.jsonl.gz` holds one Dataset per *maximal API
combination* of a product: 457 datasets from 56 products as of October 2026.

- **Identity.** `dataset_code` is `<UniqueId>|<measures>|<variables>`, with the names
  sorted, for example `T10011|avstslut|itrfslut|agarkat|ar|tillst`.
- **Measures.** Measures form one synthetic `measure` dimension with the metric role.
  Units come from a one-period `/data` probe.
- **Hierarchies.** Nested variables carry `extension.trafa.hierarchy` and `parent`.
- **Totals and markers.** `t1` is the eliminated total. Markers (`-`, `..`) and cell
  notes are passed through in `status`.
- **Native fields.** `extension.trafa` holds the product, the measure dimension, the
  covered API combinations and `source_record`.

The trafa-adapter README has the full mapping rationale.

`state/current.jsonl.gz` holds each dataset's `verified_at`, `checked_at`,
publication status and error, plus the withheld documents. `reports/latest.json`
also lists the excluded products and the combinations that have no measure (82).

## Weekly refresh

The workflow runs every Monday at 06:15 Europe/Stockholm. Trafikanalys has no
usable update signal, and a full run is about 400 requests in about a minute, so
**every run rebuilds every dataset**. An identical rebuild keeps the accepted bytes
and is counted as `unchanged`.

```powershell
uv run --extra harvest python -m harvesting.runner run --adapter trafa
uv run --extra harvest python -m harvesting.runner collect --adapter trafa
uv run --extra harvest python -m harvesting.runner run --adapter trafa --product T10011 T10033
```

### Failures and publication

- **Product list.** If the product list fails, the run fails and nothing is
  published.
- **Product endpoint.** A product whose `/structure` fails has an unavailable
  endpoint. It is retried once at the end of the run.
- **Dataset endpoint.** A dataset's `/data` unit probe is its endpoint check.
- **Metadata failures.** A failing documentation page, parse or validation is a
  metadata failure. It keeps the previous documents and removes none.
- **Publication rule.** It is shared with UKÄ and SDB in `harvesting.currency`.
  A dataset is published only while its endpoint works and its document was
  verified within 60 days. Otherwise it is withheld in state and restored
  automatically.
- **Removal.** Only an unbounded run removes datasets: those of products missing from
  the product list, or those a fully discovered product no longer lists.

## Retrieval

The `trafa` entry point works in a plain installation. It needs
`REQUIRED_METADATA = ("/id", "/extension/trafa")`.

```python
from retrieval.adapters.trafa import retrieve

fragment = await retrieve(
    session,
    provider_code="trafa",
    dataset_code=document["extension"]["nordicintel"]["dataset_code"],
    language="sv",
    selection=selection,
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"]["metadata_url"],
    metadata={
        "/id": document["id"],
        "/extension/trafa": document["extension"]["trafa"],
    },
)
```

- **Selection.** Every dimension must be selected; eliminable ones select `t1`.
- **Requests.** Queries are split to stay under Trafikanalys' 2,048-character limit
  and 20,000 rows. At most four parts are fetched at once, and nothing is retried.
- **Values.** Values are numbers. Markers and notes go to `status`.
- **Errors.** `ValueError` for invalid selections, `LookupError` for unknown items.

## Verification

```powershell
uv run --extra harvest pytest tests/trafa
uv run --extra harvest python scripts/check_trafa_live.py
```

The tests use trafa-adapter's recorded responses for T10011, T10033 and T04010. The
live script rebuilds and validates T10011 and T10033 and retrieves one small
selection from each.

# PXWeb registry additions

Implementation of the [8 October 2026 gap report](https://github.com/nordicintel/Third-party-APIs/blob/main/docs/pxweb_registry_gap_analysis.md).

The v1 provider configuration now contains 26 providers, 52 databases and 62
language/database scopes. The seven additions below use Swedish only, the existing
incremental reconciliation and one metadata JSONL per provider/language under
`data/pxweb_v1/metadata/`. Both manual live-test workflows include them; production
jobs obtain their provider matrix from `config/catalogs.json`.

| Provider code | Source | Discovered tables |
|---|---|---:|
| `goteborg` | Göteborgs Stad, two published databases | 241 |
| `karlstad` | Karlstads kommun | 137 |
| `orebro` | Örebro kommun | 39 |
| `helsingborg` | Helsingborgs stad | 34 |
| `norrkoping` | Norrköpings kommun, eight nonempty databases | 23 |
| `skog_ska22` | Skogsstyrelsen SKA22 | 18 |
| `scb_hvd` | SCB HVD-uppdrag | 32 |

SKA22 has its own provider identity because it is a separate installation from
`skog`. Hidden, unverified databases from the report are not added. The current
source-only lifecycle and separate v2 output are unchanged.

## Verification on 8 October 2026

All 15 added database/language scopes passed full discovery checks: API-form and
web-form entry points returned the same 524 records, automatic database discovery
matched the configuration, and rendered table samples matched the emitted routes.
The successful paced Göteborg rerun supersedes its initial rate-limited check.

| Provider | Metadata validated | Scope |
|---|---:|---|
| `goteborg` | 58 / 241 | Bounded; includes both databases |
| `karlstad` | 59 / 137 | Bounded |
| `orebro` | 39 / 39 | Full |
| `helsingborg` | 34 / 34 | Full |
| `norrkoping` | 23 / 23 | Full, all eight databases |
| `skog_ska22` | 18 / 18 | Full |
| `scb_hvd` | 32 / 32 | Full |

No sampled metadata failed. These checks validate complete category coverage for
each tested table, not just a successful HTTP response. The two bounded runs are
explicitly not full metadata acceptance; the configured production job will fetch
all new tables. No production metadata files were changed by verification.

Local receipts are grouped by provider in `output/registry-gaps/`: `*-metadata.json`,
the limited `*-smoke.json` checks, and discovery compatibility reports. Göteborg's
metadata count is the distinct union of its bounded run and smoke checks. These
ignored receipts are working evidence, not a new published history store.

At the registry-addition commit, the deterministic suite passed **175 tests**;
[current verification](../migrations/consolidation.md) tracks the subsequent consolidation. Ruff lint, formatting
checks and wheel/sdist build pass. Regression tests cover HVD's two displayed
categories, HVD metadata enrichment through both API lookup paths, Göteborg pacing
and the configured workflow matrices.

## SCB HVD routing

The website route `uppdrag/HV01__HV0101/HVDLE0101T01/` displays only the
`HV0101` category. Unlike the synthetic prefixes used by other PXWeb installations,
`HV01` is a real API path component. The previously constructed URL
`/api/v1/sv/uppdrag/HV0101/HVDLE0101T01` returns HTTP 400; inserting the
website's `HV01` component returns HTTP 200.

The discovery helper retains that component in API URLs specifically for
`hvduppdrag.scb.se`, database `uppdrag`. It preserves the displayed category path,
names and source dates. Metadata uses the existing GET plus category-covering
JSON-stat2 POST implementation. No invented categories or browser export fallback
are needed. The production SCB v2 configuration stays separate.

All 32 tables passed full live metadata validation on 8 October: complete category
coverage, correct identity, listing title/date and `value: []`. Web-form/API-form
discovery matched all 32 records, and automatic database discovery matched the
configured `uppdrag` scope. Local receipts are
`output/registry-gaps/scb_hvd-metadata.json` and
`output/registry-gaps/hvd-compatibility.json`.

## Göteborg request pacing

The source's `/api/v1/sv/?config` advertises ten requests per ten seconds. Filling
missing publication dates requires a flat listing and multiple parent-folder
requests. Discovery now waits 1.1 seconds before each such request on this host,
including retries and the first request of a new context. Metadata retrieval
already respects the configuration's interval. The first unpaced compatibility
run hit HTTP 429; that run is not counted as acceptance.

## Socialstyrelsen remains unavailable through PXWeb API

Live web discovery found 799 tables: 646 in E-hälsa och välfärdsteknik i kommunerna,
and 153 in Nationella indikatorer för hälso- och sjukvård. On 8 October,
`https://px.socialstyrelsen.se/api/v1/sv/` returned HTTP 404 and
`https://px.socialstyrelsen.se/pxweb/api/v1/sv/` returned the website's HTTP 500
error page. Direct table metadata URLs from both databases returned HTTP 404
under both API prefixes too. A sampled table page advertised no API URL.

The separate live SDB subject listing at `https://sdb.socialstyrelsen.se/api/v1/sv`
contains 15 subjects, but neither of these PXWeb databases. Subject-level inspection
does not establish table-level overlap, so these 799 tables are not claimed as
covered by the SDB adapter or added as a working provider here.

The report's explicitly skipped candidates remain excluded from this addition:
Umeå, Uppsala, SILO, the SLU mirror, Nordic Statistics, NHWStat and Øresund.
Västerås remains explicitly excluded by the owner's earlier decision.

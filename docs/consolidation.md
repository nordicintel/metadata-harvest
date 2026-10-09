# Integration consolidation inventory

This repository adopts the integration responsibilities in the three reviewed
repositories, including shared schema ownership and independent observation retrieval.
PXWeb v1, PXWeb v2 and Kolada are implemented here. Catalogue services, HTTP APIs, databases,
websites, file scrapers and BRÅ remain outside this implementation.

## Responsibility migration

| Source and inspected revision | Adopt here | Supersede or leave external |
|---|---|---|
| [nordicintel-backend](https://github.com/nordicintel/nordicintel-backend/tree/56bc1df45fdb81500fb3022bf31c2a386a1ead06), plus database-free work at `35b7310` | Adapter knowledge, source semantics, selection and retrieval contracts | PostgreSQL/admin API infrastructure and older paired `DatasetDocuments` contract are not imported |
| [nordicintel-project](https://github.com/nordicintel/nordicintel-project/blob/180490dc142ad7ae4ac345bec754f3c10709e9cc/docs/architecture.md) | Integration requirements, supported-source inventory and consumer boundaries | One repository per adapter and local-only execution are superseded by subpackages and resumable Actions |
| [nordicintel-schemas](https://github.com/nordicintel/nordicintel-schemas/tree/77a7a4665fea6f3c2be8f286d560b79317f08dd7) | Dataset, Provider, Retrieval Result, mappings and semantic rules | This repository owns future schemas; prepared `2.0.0` was unpublished; old identifiers are replaced with repository-owned identifiers |

Keep current grouped storage, resumable jobs and adapter-isolated publication.
No source repository is archived and no unrelated job disabled by this handover.
Imported schema licence/provenance is retained [with the schemas](../src/metadata_harvest/schemas/README.md).

## Adapter matrix

| Adapter | Provider / languages | Source evidence | Availability and output here | Remaining acceptance / preparation |
|---|---|---|---|---|
| PXWeb v1 | 26 codes below; configured languages independently | This repository baseline `060f310`; backend source knowledge | Complete metadata mapping and retrieval; shared Dataset, grouped gzip | Complete converted-corpus and renewed automated live acceptance tracked below |
| PXWeb v2 | `scb`, sv/en | This repository baseline `060f310`; SCB production API | Metadata and codelists, common Dataset and standalone v2 retrieval | Complete table acceptance enables schedule; dependency errors remain separate |
| SDB | `socialstyrelsen`, sv/en with asymmetric coverage | [sdb-adapter `344212d`](https://github.com/nordicintel/sdb-adapter/tree/344212d51a28b848288e2beb90b7a5432bfa225e) | Unavailable placeholder here; metadata/retrieval implemented externally | [Preparation](adapters/sdb.md) |
| Trafikanalys | `trafa`, sv/en coverage requires verification | [trafa-adapter `936b20e`](https://github.com/nordicintel/trafa-adapter/tree/936b20eadd4ba9e748d6fb00c6ca67a7e414a290) | Unavailable here; metadata/retrieval implemented externally | [HTML discovery boundary](adapters/trafa.md) |
| Kolada | `kolada`, sv; municipality/groups and OU | One-time trusted initialization; no runtime repository dependency | Metadata, targeted incremental refresh and standalone retrieval implemented; live acceptance recorded below | [Adapter guide](adapters/kolada.md) |
| RowStore | Provider identity unresolved; language source-dependent | [rowstore-adapter `27d6991`](https://github.com/nordicintel/rowstore-adapter/tree/27d6991ba649171c64d548a6cf7ef1f40d853bbf) | Unavailable scaffold; reusable external transport | [Semantics and pagination](adapters/rowstore.md) |
| UKÄ | `uka`, Swedish source; English coverage unverified | [uka-adapter `ca533a7`](https://github.com/nordicintel/uka-adapter/tree/ca533a715e7774a757056afb7cf55db80092f4dd) | **Implemented externally**, unavailable here | [CSV-export scope decision](adapters/uka.md) |

External implementation is not support in this distribution. All four placeholders
raise `NotImplementedError`, are excluded from production jobs and have no entry points.
Their future common output target is the shared Dataset/retrieval contract.

## Configured PXWeb v1 provider coverage

The configuration below is distinct from successful current live acceptance.

| Provider code | Languages | API root |
|---|---|---|
| `mi` | sv | https://www.mi.se/PXWeb/api/v1 |
| `csn` | sv | https://statistik.csn.se/PXWeb/api/v1 |
| `energi` | sv, en | https://pxexternal.energimyndigheten.se/api/v1 |
| `fohm` | sv, en | https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/api/v1 |
| `sjv` | sv | https://statistik.sjv.se/PXWeb/api/v1 |
| `konj` | sv, en | https://prognos.konj.se/PxWeb/api/v1 |
| `konjbar` | sv, en | https://statistik.konj.se/PxWeb/api/v1 |
| `lansstyr` | sv | https://pxweb.lansstyrelsen.se/api/v1 |
| `mcf` | sv, en | https://statistik.mcf.se/PXWeb/api/v1 |
| `slu` | sv, en | https://skogsstatistik.slu.se/api/v1 |
| `skog` | sv, en | https://pxweb.skogsstyrelsen.se/api/v1 |
| `domstol` | sv, en | https://pxweb.etjanst.domstol.se/PxWeb/api/v1 |
| `linkoping` | sv | https://statistik.linkoping.se/pxweb/api/v1 |
| `kulturanalys` | sv | https://statistik.kulturanalys.se/api/v1 |
| `ehm` | sv | https://statistik.ehalsomyndigheten.se/api/v1 |
| `tillvaxtanalys` | sv | https://statistik.tillvaxtanalys.se/PxWeb/api/v1 |
| `vgregion` | sv | https://pxweb2022.vgregion.se/Pxwebb/api/v1 |
| `sundsvall` | sv | https://m02-http-pxwebb.login.sundsvall.se/PXWeb_Ext/api/v1 |
| `skolverket` | sv | https://statistikdatabasen.skolverket.se/PxWeb/api/v1 |
| `goteborg` | sv | https://statistikdatabas.goteborg.se/api/v1 |
| `karlstad` | sv | https://pxweb2.karlstad.se/api/v1 |
| `orebro` | sv | https://statistik.orebro.se/api/v1 |
| `helsingborg` | sv | https://statistik.helsingborg.se/PxWeb/api/v1 |
| `norrkoping` | sv | https://statistik.norrkoping.se/PxWeb/api/v1 |
| `skog_ska22` | sv | https://pxwebska.skogsstyrelsen.se/api/v1 |
| `scb_hvd` | sv | https://hvduppdrag.scb.se/api/v1 |

See [registry gaps](pxweb-registry-gaps.md) for HVD routing and original addition checks.
The HVD helper preserves the API's hidden parent path rather than guessing from the website.

## Verification evidence

The pre-consolidation baseline passed 175 deterministic tests. It contained 6,834 v1
and 8,620 v2 accepted metadata documents; those documents used the legacy identity
and did not conform to the adopted shared schema. Native-source coverage evidence
is not canonical-schema acceptance.

The explicit candidate conversion validates all records, identity and complete
category order/count preservation before publication. Latest conversion evidence is recorded with the documentation in
[verification/](verification/pxweb_v1-conversion.json), outside collector-owned state.
New fixture and local HTTP tests cover contracts, protocol splitting/coordinate
assembly, lifecycle isolation and packaging. Full production acceptance requires
complete automated runs and subsequent incremental reuse, with every upstream
failure accounted for in current reports; bounded checks alone do not establish it.

The old v2 run accepted 8,620 tables and 1,956 codelists with 55 separately reported
upstream codelist failures. Those dependency failures do not revoke table acceptance
or prevent scheduling under the agreed gate. The old v1 report was incomplete,
including MCF ContentsCode mismatches, two energy API HTTP 400s and an SJV listing
failure; these require renewed checks and must not be relabelled as success.

Status claims and verification links are updated with implementation delivery;
latest machine-readable [v1](../data/pxweb_v1/reports/harvest.json) and
[v2](../data/pxweb_v2/reports/harvest.json) reports remain authoritative for run outcomes.

### Shared-contract delivery verification (9 October 2026)

All **15,454** existing metadata documents passed candidate conversion and identity,
dimension/category order/count and source-record preservation checks: 6,834 v1 and
8,620 v2. V1 metadata occupies 6.35 MiB compressed across 27 collections; v2 occupies
46.25 MiB across two, largest 24.96 MiB. No collection was split or truncated.

The complete deterministic suite passes **225 tests**; Ruff lint/format, generated
reference checks and package build pass. Wheel and sdist include all three schemas
and licence; an isolated plain wheel install loads all **27 provider entry points**
with no HTTPX, Playwright or jsonschema installed or imported.

Bounded live metadata-to-retrieval checks pass for all **26 configured v1 providers**
and SCB **sv/en**, including HVD routing, MCF singleton ContentsCode and Sundsvall's
anonymous gateway bootstrap. Summaries contain no observations:
[v1 evidence](verification/pxweb_v1-retrieval.json),
[v2 evidence](verification/pxweb_v2-retrieval.json).
These checks do not replace the required full automated run and incremental run;
renewed production acceptance remains pending at this commit.

### Harvest visibility and execution correction (9 October 2026)

The full SCB acceptance run
[37896314446](https://github.com/nordicintel/metadata-harvest/actions/runs/37896314446)
was cancelled at the user's request; queued v1 run 37896317776 was cancelled too.
The saved SCB snapshot at 07:30:35 UTC contains 1,443 accepted refreshed tables,
zero table failures, 7,177 pending tables and 222 refreshed codelists. Its 55
unresolved codelist references include only two attempted failures (HTTP 404);
the remaining 53 had not yet been fetched. This is partial evidence, not complete
acceptance. The cancelled run's uploaded artifact contains its resumable checkpoint.

Workers now publish live counters and individual errors through the Checks API,
with preflight read-back, separate stage counts, heartbeat/staleness and measured
queue estimates. The inspector retrieves every error and reconstructs large
messages across annotation chunks. CI tests visibility while a short synthetic
worker is still active; it does not contact providers or restart the full harvest.
Adapters use independent harvest queues and a shared collector publication lock.
SCB pacing follows `/config` without the former 0.5-second minimum. Incremental
reuse, checkpoint continuation, resource isolation and adapter-scoped publication
remain required and covered by regression tests. See [operations](harvesting.md).

Live Checks API visibility was verified during the synthetic worker in
[37900778026](https://github.com/nordicintel/metadata-harvest/actions/runs/37900778026):
one accepted task, one remaining task and the complete individual codelist error
were read while the worker and check were still in progress. This verifies the
monitoring transport, not production harvest completion.

The subsequent MCF category mismatch was an adapter defect: a single selected
`ContentsCode` can be serialized as `EliminatedValue` even when the variable has
several available categories. Harvesting and standalone retrieval now restore the
explicitly selected code while preserving all categories, units and notes. Live
checks passed for `B13`, `B22`, `B34` and `B40` in both languages, including retrieval
of a non-first contents category: [evidence](verification/mcf-contents-selection.json).
The full local suite passed 239 tests; lint, formatting and package build passed.
Existing running workers retain their checked-out code; this fix does not restart them.

## Kolada delivery — 9 October 2026

Kolada is implemented and published with 6,516 current metadata documents in one 35.6 MiB
compressed collection. Initial data was imported once as trusted input; no seeding
feature, external repository dependency, data.eu enrichment or observation store ships.
Full native reference catalogues are retained once in compact current state.

Only nine seeded documents across six representative KPIs were validated. All nine
bounded live retrievals passed, including municipality groups, gender and OU data;
initial upstream timeouts passed on targeted retry. Two six-KPI incremental runs
completed without errors: first eight reused and one metadata-only update, then nine
reused and zero updates. Their unchanged metadata bytes were verified identical.
Neither incremental run fetched KPI observations. This is sample-based acceptance,
not a claim of fresh live verification or schema validation of every imported document.

The complete [hosted suite](https://github.com/nordicintel/metadata-harvest/actions/runs/37910795515)
passed 268 tests, including all 11 lifecycle tests. Lint, formatting, schema-reference checks,
wheel/sdist build and a plain installation loading all 28 provider retrieval entry
points passed. [Evidence](verification/kolada.json) contains only selections, counts
and outcomes, never observations. The bounded [hosted run](https://github.com/nordicintel/metadata-harvest/actions/runs/37909830351)
and publication passed with nine reused documents, zero updates and zero errors.
The independent daily incremental schedule was enabled on 9 October 2026 and runs
at 06:30 Europe/Stockholm.

## Shared catalog and schedule ownership

Accepted adapter files remain the source layer. The independent shared catalog
consumes those files, preserving grouping and source errors. Content enrichment and
semantic enrichment have typed, ordered proposal interfaces but are explicitly
inactive; no external lookup, automatic assignment or AI implementation is claimed.

The catalog cannot dispatch or perform harvesting. Adapter publication dispatches
catalog construction in one direction. Manual catalog construction is independent.
[schedules.toml](../schedules.toml) replaces the old adapter enablement variables and
scattered schedule decisions; CI checks generated workflow blocks for drift.

The [catalog guide](catalog.md) and [operations guide](harvesting.md) are the current
operational references. Earlier verification entries are dated evidence,
not substitutes for the latest reports or current config. Acceptance for this change
uses already accepted collections without new harvesting or corpus validation.

Local acceptance copied 39 grouped collections, then reused all 39 byte-for-byte;
the largest compressed file was 37,302,903 bytes. Both stages reported
`not_configured`; upstream errors remained visible. No new harvest or per-document
validation was performed. All 287 tests, lint/format, schema-reference checks,
wheel/sdist build and the plain-installation check passed. An independent
GPT-6.1 Sol Medium documentation review completed with its findings resolved.
[Verification evidence](verification/catalog.json) records the acceptance results.

Hosted [CI](https://github.com/nordicintel/metadata-harvest/actions/runs/37918665145)
passed 287 tests and package checks. The first
[catalog run](https://github.com/nordicintel/metadata-harvest/actions/runs/37918683309)
built and published all 39 collections; the
[repeat](https://github.com/nordicintel/metadata-harvest/actions/runs/37918806748)
reused all 39 and changed only the report. Both workflows deliberately reported
incomplete coverage after successful publication because existing upstream errors
remain visible. No harvester was started by either catalog run.

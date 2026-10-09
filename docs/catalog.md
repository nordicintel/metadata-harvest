# Catalog processing and enrichment placeholders

The shared catalog is built from the latest **committed accepted adapter files**.
It does not contact source APIs, refresh listings, run harvesters, or continue their
checkpoints. Scheduled or deliberately manual harvesting is a separate operation.

## Interfaces and order

1. `metadata_harvest.processing.content_enrichment.process(dataset)` reserves
   external matching, Dataportal/data.eu attributes and documentation content.
2. `metadata_harvest.processing.semantic_enrichment.process(dataset)` reserves
   role recognition and derived period/time-unit assignments after content additions.

Both return `StageResult(status="not_configured")` with empty changes, evidence
and errors. No external content, rules or JEV integration ships. Do not describe
this pass-through as successful enrichment.

`process_dataset(dataset)` runs the stages in order on independent copies. A
`StageResult` can propose JSON Pointer field replacements under existing parent
objects, supporting evidence and individual errors. Completed changes are applied
only after common metadata validation and source identity/category/evidence checks.
A rejected proposal retains that stage's input; later stages and other datasets
remain independent. These semantics are fixture-tested, not live enrichment.

The current bulk builder requires both stages to remain inactive. It deliberately
copies trusted collections without invoking a per-document processor. Activating
real enrichment requires implementing incremental document/dependency processing
explicitly; changing a flag cannot silently claim enrichment or trigger a corpus scan.

## Build and publication

```powershell
# On a fresh checkout, download the previous release pinned by catalog state.
uv run python -m metadata_harvest.catalog restore
# Offline build from committed adapter results into a temporary candidate.
uv run python -m metadata_harvest.catalog build
# Publish verified Release assets, then commit/push catalog state and reports.
uv run python -m metadata_harvest.catalog publish
# Or run the hosted build + publication workflow.
gh workflow run catalog.yml --ref main
```

Default inputs are `data/<adapter>/metadata/`, optional `codelists/`, and latest
`reports/`. Listing state, harvester work checkpoints and worker artifacts are
excluded. Uncommitted source changes are rejected by the commands. Candidates live
under ignored `output/catalog/` and can be rebuilt; they are not historical snapshots.

Each GitHub Release contains four files:

| File | Contents |
|---|---|
| `catalog.jsonl.gz` | All accepted Dataset records across adapters, providers and languages |
| `codelists.jsonl.gz` | All accepted native codelist records, retaining `provider_code`, `language` and `codelist` |
| `current.json` | Accepted collection hashes, compressed member offsets, asset hashes and release tag |
| `latest.json` | Coverage, collection errors, enrichment status and upstream reports |

Dataset identity remains provider/dataset/language. Records keep their existing
shape and native extensions. Codelists are separate because they are reference
records, not Datasets. Codelist references remain in their original namespaces.

Only `data/catalog/state/current.json` and `data/catalog/reports/latest.json` are
committed. The two combined payloads are ignored local files, downloaded by
`restore` on fresh checkouts. Adapter-owned grouped collections remain unchanged.
The first successful promotion removes the old catalog's redundant grouped copies;
their accepted content is already present in the verified combined assets.

The assets concatenate complete gzip members in source-path order, preserving
every accepted compressed byte. Standard gzip readers, including Python's
`gzip.open`, decode all members into one JSONL stream. Consumers must read to EOF,
not stop after the first gzip member. This uses bounded memory and avoids a full
recompression on every build; it is not a ZIP archive.

Unchanged accepted hashes reuse bytes without decompressing or validating datasets.
The initial migration checks gzip framing once, including legacy fallback files.
New or changed collections receive a gzip integrity and final-newline check, not
per-document JSON or schema validation. This is an explicit trust boundary: malformed metadata inside an
otherwise intact accepted gzip is the adapter's responsibility. In particular, the
trusted Kolada import is not revalidated or rediscovered for catalog construction.

## Failures, deletions and coverage

A missing or corrupt collection retains its last accepted gzip member and records an error;
other collections can advance. First-time unavailable input remains absent and
reported. Removing records inside a valid accepted collection removes them from the
catalog. Missing an entire collection is not deletion evidence; publish an empty
accepted collection when the last record is confirmed removed.

Table and codelist failures remain independent. Upstream latest reports are included
verbatim, so incomplete or failed harvesting remains visible even when accepted
files copy successfully. An absent upstream report is labelled `unknown` coverage. Stage
status remains `not_configured`; catalog construction cannot certify enrichment.

The previous release assets are checked against committed state before reuse. A
missing or corrupt previous asset blocks the build rather than dropping retained
records. The `restore` command downloads only the exact pinned release and verifies
both asset hashes before replacement.

Candidates carry input, previous-catalog and output hashes. Changed inputs, changed
catalog state or tampered candidates prevent promotion. Rebuild from accepted files;
never fix staleness by starting a harvest. Publication uses the shared queue. All
four assets upload to a content-addressed draft release; their remote SHA-256
digests must match before the release is published. Only then are catalog state
and reports committed with scoped staging and no force push. Interrupted draft
uploads can be retried. Published assets are never overwritten. If Git publication
fails, the previously committed release remains the authoritative recovery source.
Unchanged inputs produce identical assets, release tag and coverage report.
Transient copy/reuse counters are returned as `build_counts` by the build command;
the published report records stable collection and retention counts.

The 100 MiB Git file-size gate still protects tracked collections, state and reports;
the combined payloads use Release asset storage instead. No metadata is split or
omitted to meet Git's limit. Error reports publish before the hosted job reports failure.

Download the release tag recorded in committed catalog state for a reproducible
snapshot, or use the repository's latest Release for the newest complete upload.
Private repository access requires authentication; publishing a Release does not
make this repository or its data public.

Adapter collectors dispatch catalog construction after final publication, including
publication with isolated errors. A failed dispatch is reported after allowing any
harvester continuation; rerun the catalog workflow manually without repeating harvests.
There is no reverse dispatch, catalog schedule or
catalog publication loop. A scheduled harvest still runs only through its own
workflow and [central configuration](../schedules.toml).

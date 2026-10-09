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
# Offline build from committed adapter results into a temporary candidate.
uv run python -m metadata_harvest.catalog build
# Deliberately promote and commit/push only data/catalog/ to main.
uv run python -m metadata_harvest.catalog publish
# Or run the hosted build + publication workflow.
gh workflow run catalog.yml --ref main
```

Default inputs are `data/<adapter>/metadata/`, optional `codelists/`, and latest
`reports/`. Listing state, harvester work checkpoints and worker artifacts are
excluded. Uncommitted source changes are rejected by the commands. Candidates live
under ignored `output/catalog/` and can be rebuilt; they are not historical snapshots.

Final files retain their grouping under `data/catalog/<adapter>/metadata/` and
`codelists/`. Identity remains provider/dataset/language. State contains accepted
collection hashes and the latest report contains processing status, individual
collection errors, stage status and upstream reports.

Unchanged accepted hashes reuse bytes without decompressing or validating datasets.
New or changed collections receive a gzip integrity check, not per-document JSON or
schema validation. This is an explicit trust boundary: malformed metadata inside an
otherwise intact accepted gzip is the adapter's responsibility. In particular, the
trusted Kolada import is not revalidated or rediscovered for catalog construction.

## Failures, deletions and coverage

A missing or corrupt collection retains its last catalog copy and records an error;
other collections can advance. First-time unavailable input remains absent and
reported. Removing records inside a valid accepted collection removes them from the
catalog. Missing an entire collection is not deletion evidence; publish an empty
accepted collection when the last record is confirmed removed.

Table and codelist failures remain independent. Upstream latest reports are included
verbatim, so incomplete or failed harvesting remains visible even when accepted
files copy successfully. An absent upstream report is labelled `unknown` coverage. Stage
status remains `not_configured`; catalog construction cannot certify enrichment.

Candidates carry input, previous-catalog and output hashes. Changed inputs, changed
catalog state or tampered candidates prevent promotion. Rebuild from accepted files;
never fix staleness by starting a harvest. Publication uses the shared queue and
commits catalog files/reports together, with scoped staging and no force push.
The file-size gate refuses collections at or above 100 MiB without splitting or
omitting metadata. Error reports publish before the hosted job reports failure.

Adapter collectors dispatch catalog construction after final publication, including
publication with isolated errors. A failed dispatch is reported after allowing any
harvester continuation; rerun the catalog workflow manually without repeating harvests.
There is no reverse dispatch, catalog schedule or
catalog publication loop. A scheduled harvest still runs only through its own
workflow and [central configuration](../schedules.toml).

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

Install the native 7-Zip command (`7zz` or `7z`). On Windows the standard
`Program Files/7-Zip/7z.exe` installation is also detected. Hosted catalog and
verification workflows install Ubuntu's `7zip` package.

```powershell
# Offline build from committed accepted adapter results.
uv run python -m metadata_harvest.catalog build
# Promote and commit/push only data/catalog/ to main.
uv run python -m metadata_harvest.catalog publish
# Or run the hosted build + publication workflow.
gh workflow run catalog.yml --ref main
```

Default inputs are `data/<adapter>/metadata/`, optional `codelists/`, and latest
`reports/`. Listing state, harvester checkpoints and worker artifacts are excluded.
Uncommitted source changes are rejected. Candidates live under ignored
`output/catalog/` and can be rebuilt.

The final catalog consists of two archives committed directly to Git:

| File | Contents |
|---|---|
| `data/catalog/catalog.7z` | `catalog.jsonl`: all accepted Dataset records across adapters, providers and languages |
| `data/catalog/codelists.7z` | `codelists.jsonl`: all native codelist records with their provider and language |

Extract each archive with 7-Zip before reading its JSONL file. Dataset identities,
record shapes, native extensions and codelist references remain unchanged. Each
line in the catalog is a Dataset; codelists stay separate reference records.
There is no release upload, release download or external artifact store.

`state/current.json` records accepted input hashes, archive hashes and each source
collection's uncompressed JSONL byte range/hash. `reports/latest.json` records
coverage, errors and enrichment status. Adapter-owned grouped files are unchanged.
The first successful promotion removes the old catalog's redundant grouped copies
only after the complete archives and state have been promoted.

Archives use LZMA2, compression level 7, a 64 MiB dictionary, two threads and no
filesystem timestamps. Input paths and JSONL records retain deterministic order.
An unchanged archive is reused byte-for-byte without extraction or recompression;
a codelist-only change does not rebuild the metadata archive. Changed archives
use temporary plain JSONL files and bounded-memory copying. Allow temporary disk
space for the combined plain file and, when retaining failed input, its extracted
previous version. Temporary files are removed when construction finishes or fails.

New or changed collections receive gzip integrity and final-newline checks, not
per-document JSON or schema validation. Initial migration checks legacy framing
once. Trusted metadata semantics remain the adapter's responsibility; the catalog
never rediscovers or schema-validates the Kolada corpus.

## Failures, deletions and coverage

A missing or corrupt input retains its previous accepted JSONL range while other
collections advance. First-time unavailable input stays absent and is reported.
Missing an entire collection is not deletion evidence: publish an empty accepted
collection when the last record is confirmed removed. Byte ranges and hashes allow
retention without keeping duplicate per-provider catalog files.

Prior archive hashes must match catalog state before reuse. Missing or corrupt
prior archives block construction rather than silently losing accepted records.
Recover those files from Git, then rebuild; no network restore command is needed.

Candidates carry input, previous-catalog and output hashes. Changed inputs, changed
catalog state or tampered candidates prevent promotion. Both archives and the
state/report files must remain below the existing 100 MiB per-file gate. An oversized
candidate fails before promotion; records are never split or omitted to fit.
Publication remains scoped, serialized and non-force-pushed.

Unchanged inputs produce identical archives, state and coverage reports. Transient
copy/reuse counters are returned as `build_counts`; published counts describe the
accepted collections and retained inputs. Table and codelist failures remain
independent. Latest upstream reports are retained verbatim, including failures;
absent reports mean unknown coverage. Enrichment stages remain `not_configured`.

Adapter collectors dispatch catalog construction after final publication, including
publication with isolated errors. A failed dispatch is reported after allowing any
harvester continuation; rerun the catalog workflow manually without repeating harvests.
There is no reverse dispatch, catalog schedule or
catalog publication loop. A scheduled harvest still runs only through its own
workflow and [central configuration](../schedules.toml).

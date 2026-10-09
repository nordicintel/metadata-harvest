# Harvest jobs, scheduling and publication

Three independent adapter workflows own source requests. Collectors share the
`production-publication` queue, check out current `main` after acquiring it, and
stage only their adapter directory. The catalog uses the same publication queue
but never dispatches a harvester or makes source requests.

## One schedule configuration

Edit [schedules.toml](../schedules.toml), then run and commit both the config and
its generated workflow changes:

```powershell
uv run --extra harvest python -m metadata_harvest.scheduling sync
uv run --extra harvest python -m metadata_harvest.scheduling sync --check
```

CI runs the check command. Do not hand-edit the marked schedule/enablement blocks
in workflow YAML. Only those blocks are generated; worker logic remains adapter-owned.
The previous adapter enablement repository variables are no longer read.

The top-level `timezone` is an IANA timezone, currently `Europe/Stockholm`.
Each implemented adapter has `enabled = true` or `false`, and schedule entries
with a numeric five-field POSIX `cron` and `mode = "incremental"` or `"full"`.
Fields mean minute, hour, day-of-month, month, day-of-week; Sunday is 0.
Lists, ranges, wildcards and steps are supported. Use disjoint schedules so two
entries do not request the same run. GitHub scheduling may be delayed; it is not
a real-time timer. Timezone-aware schedules follow local daylight-saving changes.

| Adapter | Default local schedule | Mode |
|---|---|---|
| PXWeb v1 | Daily 04:15 | Incremental |
| SCB PXWeb v2 | Monday–Saturday 06:15; Sunday 06:15 | Incremental; Sunday full |
| Kolada | Daily 06:30 | Incremental only |

The config contains current enablement. SCB stays disabled until complete table
acceptance; independent codelist failures do not block that acceptance gate.
Disabling a schedule does not cancel active jobs or disable manual operations.
`HARVEST_PARALLEL_PROVIDERS` remains a separate operational control (default 19).

## Deliberate manual operations

Use GitHub Actions **Run workflow**, or these explicit commands:

```powershell
# Fresh incremental source checks; these contact provider APIs/websites.
gh workflow run harvest.yml --ref main -f full=false
gh workflow run harvest-pxweb-v2.yml --ref main -f full=false
gh workflow run harvest-kolada.yml --ref main
gh workflow run harvest-uka.yml --ref main
gh workflow run harvest-sdb.yml --ref main
# Deliberate PXWeb reharvest, including unchanged tables.
gh workflow run harvest-pxweb-v2.yml --ref main -f full=true
gh workflow run harvest-uka.yml --ref main -f full=true
# Catalog only: consumes existing accepted files and never contacts source APIs.
gh workflow run catalog.yml --ref main
```

V1 also supports `full=true`; Kolada intentionally has no full option. Leave
`resume_run` empty for normal manual runs. Internal continuations carry the run ID
and retain checkpointed mode; they are not a new scheduled/manual refresh.
Changing a schedule affects future runs only. See [catalog operations](catalog.md)
for local build/publication commands and partial-coverage reporting.

## Inspect a running job

Each worker creates a **Harvest progress** GitHub check and verifies it can be
read back **before starting source requests**. An independent supervisor updates
that check every 30 seconds, including while a request is waiting or retrying.
This does not depend on unfinished Actions logs or end-of-job artifact uploads.
The check's details show phase, current resource/language, processed/total,
accepted, failed, reused, remaining, codelist successes and separate errors by
stage. Counts refer to this worker wave; v2 also reports previously completed
checkpoint tasks. Discovery totals and ETA remain unknown until available.

Read live counters or every individual error using the installed package and
authenticated GitHub CLI:

```powershell
uv run python -m metadata_harvest.monitor status RUN_ID
uv run python -m metadata_harvest.monitor status RUN_ID --errors
uv run python -m metadata_harvest.monitor status RUN_ID --worker pxweb_v2-scb --error 1
```

CLI output escapes non-ASCII characters as JSON Unicode sequences, so redirected
error inspection also works with Windows legacy console encodings without losing text.

Every error has its own numbered annotation containing adapter, provider,
language, resource identity, stage, timestamp, exception and request URL where
known. Error inspection paginates all annotations, not just recent examples.
The API endpoint is `repos/nordicintel/metadata-harvest/check-runs/CHECK_ID/annotations`;
`gh api --paginate` can inspect it directly. Error numbers are local to a worker.
Large errors are transported in numbered annotation chunks and reconstructed by
the inspector; incomplete transmission is labelled explicitly. Retries with an
ambiguous API acknowledgement can repeat an annotation; the inspector deduplicates
by worker and error number. Terminal upstream failure is distinct from
an internally retried request. A codelist failure is never a table failure.

`reported_at` is the supervisor heartbeat; `updated_at` is the last worker event.
The status command marks an unfinished check stale after 90 seconds without a
heartbeat, or when its workflow has already ended. A recent heartbeat with an old
worker event means the process is alive but making no reported progress; inspect
the current operation rather than assuming success. ETA uses measured completed
task throughput after at least five tasks, includes time spent on their dependencies,
and excludes final collection/publication. It is an estimate, never a promise.

Three consecutive telemetry publication failures stop the worker, preserving
existing snapshots. A failed or cancelled worker cannot automatically dispatch a
continuation. Cancellation also prevents collectors from publishing. Worker
telemetry (`progress.json` and one `progress.errors.jsonl`) is uploaded separately
for seven days; it never enters the metadata collections or Git publication.
The CI visibility test uses a short synthetic worker and proves that its counters
and an individual error can be read **before it exits**, without harvesting APIs.

Normal manual runs are incremental and resume unfinished work. Do not restart
completed tasks to test unrelated code. A manual full refresh needs an explicit
reason and runtime estimate; the configured Sunday refresh remains intentional.
Before resuming a cancelled v2 run, recover its `pxweb-v2-ATTEMPT` artifact and
validate it with `collect --output RECOVERED_DIRECTORY` against the unchanged
published base. This preserves accepted tasks in `work/current.json`; do not
discard that artifact and launch another full refresh. Stale artifacts must be
reconciled explicitly, never forced past validation.

## Current output only

V1 owns `data/pxweb_v1/`; v2 owns `data/pxweb_v2/`. Neither adapter publishes
into the other adapter's directory.

| Path within the adapter directory | Purpose |
|---|---|
| `metadata/` | One deterministic `.jsonl.gz` collection per provider/language |
| `codelists/` (v2) | One `.jsonl.gz` per provider/language; complete native codelist payloads |
| `table-records/` | One JSONL per provider/language for current discovery state |
| `reports/refresh.json` | Latest listing result and completeness/errors; overwritten |
| `reports/harvest.json` | Latest processing result and counts; overwritten |
| `work/current.json` | Unfinished work only; removed when complete |

Dataset identity is provider + dataset code + language. Collection filenames
escape identifiers and add a checksum to avoid Windows case collisions. Each
JSONL line is one dataset document. V1 groups multiple known source locations
in `paths`, with the newest source timestamp determining the primary endpoint.
All implemented adapters map to the shared Dataset contract and retain complete source evidence under
their adapter namespace. See [output mapping](output-contract.md).

**Confirmed-absent datasets are deleted.** There is no inactive directory,
reactivation state, dated output collection or application-level version history.
If a removed dataset appears again, it must be fetched anew.

A timeout, missing worker or incomplete traversal does not prove deletion.
Those failures are reported and leave the previous metadata pending verification.
This prevents a temporary provider outage from emptying the published collection.
An upstream modification time is distinct from a harvest/report timestamp.

## V1 lifecycle

1. Refresh configured provider/language/database scopes, preserving observed IDs
   and completeness evidence even when some rows fail.
2. Reconcile current listings: reuse unchanged documents, queue changed/missing
   metadata and delete confirmed-absent datasets from both output and listing state.
3. Workers build complete metadata. Collectors validate successful replacements,
   merge them into the provider/language JSONL and report failures visibly.

The last successfully written dataset timestamp controls incremental refresh.
Failed replacements do not advance it. Each collection is replaced atomically;
collectors alone commit the completed changes to `main`, with bounded rebase
retries and no force pushes. A failed collector must not publish a partial tree.

Workers stop before their deadline and upload completed work. Pending tasks can
continue using the same current checkpoint. Three waves without progress stop
automatic continuation with an `interrupted` status. Use **Run workflow** to
resume; do not hand-edit the checkpoint. Terminal failures retry during the next
full discovery. The checkpoint is deleted when no pending tasks remain.

Reports count `reused`, `created`, `updated`, `removed`, `deferred`, `failed` and
`unfinished`. Status is `running`, `success`, `completed_with_errors` or
`interrupted`. A failed run remains visibly failed even when other tables succeed.
GitHub Actions artifacts are temporary job transport, not a metadata archive.

## V2 lifecycle

A new run discovers both complete language catalogues, including discontinued
but still accessible tables. Incremental runs compare current catalogue records
with successfully accepted records; full runs fetch every table and referenced
codelist. Unchanged valid codelists are reused during dependency-only retries.

A failed codelist never fails valid table metadata. It is recorded separately in
`counts.codelists_failed` and retried in later runs. Collection publishes validated
successes and current errors together, then the workflow reports incomplete status
if errors remain. Missing references require corresponding explicit errors.

Workers batch deterministic gzip output, preserve full/incremental mode across
continuations and remove completed checkpoints. The collector rejects stale input
fingerprints, invalid artifacts and files at or above GitHub's 100 MiB limit.
It never splits collections or omits content to pass publication. See the
[v2 adapter guide](pxweb-v2.md) for failure and deletion rules.

## Local execution

The runner defaults to `--adapter pxweb_v1`, `providers.json` and
`data/pxweb_v1/`. V1 commands are `prepare`, `refresh`, `collect-refresh`, `build`,
`collect-metadata`, `publish` and `continue`. `prepare` prints the next phase
without writing; worker/collector commands require the run identity and relevant
provider or artifact inputs. The workflow supplies these values.

Selecting `--adapter pxweb_v2` switches defaults to `providers.pxweb_v2.json`,
`data/pxweb_v2/` and `output/pxweb_v2/`. Its commands are `run`, `collect`, `publish`
and `continue`:

```powershell
uv run --extra harvest python -m metadata_harvest.runner run --adapter pxweb_v2
uv run --extra harvest python -m metadata_harvest.runner collect --adapter pxweb_v2
```

`run` writes worker output, `collect` validates and applies it locally, and `publish`
commits/pushes the selected adapter directory. Use a separate `--data` directory
for experiments and an empty `--output` directory for each v2 worker invocation.
Run `uv run --extra harvest python -m metadata_harvest.runner --help` for supported arguments.

Run `uv run --extra harvest ruff check .`, `uv run --extra harvest ruff format --check .`, and `uv run --extra harvest pytest`.
Tests cover adapter separation, confirmed removal, incomplete discovery, failed
replacements, bounded status files, continuation, stable JSONL and safe publication.
See [the output contract](output-contract.md) and [source inventory](consolidation.md).

A failed forced v1 replacement is retried even when the listing timestamp remains
unchanged. Its unresolved error survives an intervening incomplete discovery and
is cleared by a successful replacement or confirmed removal; retained metadata is
never silently counted as a successful refresh. Reports record the full-refresh mode.

## Kolada and catalog lifecycle

Kolada refreshes native change signals and only rebuilds affected KPI scopes;
see its [adapter guide](adapters/kolada.md). Its current state is compact and its
trusted initial data is never subjected to repeated corpus validation.

After a final adapter collector publishes accepted results and errors, it explicitly
dispatches the catalog workflow before reporting any incomplete-harvest failure.
The catalog does not trigger itself. Upstream errors remain in its coverage report,
while unrelated accepted collections can advance. A failed catalog job can be
retried independently without repeating provider requests.

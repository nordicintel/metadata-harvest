# Harvest jobs and publication

The two production workflows are independent and share the `production-harvest`
queue. Collectors publish to `main` and stage only their adapter's data directory.

| Adapter | Workflow | Schedule (Europe/Stockholm) | Enablement |
|---|---|---|---|
| `pxweb_v1` | Scheduled harvest | Daily 04:15 | Enabled unless `HARVEST_ENABLED=false` |
| `pxweb_v2` | SCB PXWeb v2 metadata | Daily 06:15; Sunday full, otherwise incremental | Scheduled runs require `PXWEB_V2_ENABLED=true` |

Both can run manually. V2 manual dispatch exposes a `full` option. As checked on
9 October 2026, v1 scheduling is enabled; v2 scheduling remains disabled because
55 upstream codelist errors prevent the original zero-error acceptance gate.
V1 provider parallelism is controlled by `HARVEST_PARALLEL_PROVIDERS` (default 19,
not the provider count). V2 uses one rate-limited worker for both languages.

## Current output only

V1 owns `data/pxweb_v1/`; v2 owns `data/pxweb_v2/`. Neither adapter publishes
into the other adapter's directory.

| Path within the adapter directory | Purpose |
|---|---|
| `metadata/` | One collection per provider/language: v1 `.jsonl`, v2 `.jsonl.gz` |
| `codelists/` (v2) | One `.jsonl.gz` per provider/language; complete native codelist payloads |
| `table-records/` | One JSONL per provider/language for current discovery state |
| `reports/refresh.json` | Latest listing result and completeness/errors; overwritten |
| `reports/harvest.json` | Latest processing result and counts; overwritten |
| `work/current.json` | Unfinished work only; removed when complete |

Dataset identity is provider + dataset code + language. Collection filenames
escape identifiers and add a checksum to avoid Windows case collisions. Each
JSONL line is one dataset document. V1 groups multiple known source locations
in `paths`, with the newest source timestamp determining the primary endpoint.
V2 retains the complete catalogue record and native SCB metadata fields.

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
uv run python -m metadata_harvest.runner run --adapter pxweb_v2 --full
uv run python -m metadata_harvest.runner collect --adapter pxweb_v2
```

`run` writes worker output, `collect` validates and applies it locally, and `publish`
commits/pushes the selected adapter directory. Use a separate `--data` directory
for experiments and an empty `--output` directory for each v2 worker invocation.
Run `uv run python -m metadata_harvest.runner --help` for supported arguments.

Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
Tests cover adapter separation, confirmed removal, incomplete discovery, failed
replacements, bounded status files, continuation, stable JSONL and safe publication.
See [the output contract](output-contract.md) and [source inventory](consolidation.md).

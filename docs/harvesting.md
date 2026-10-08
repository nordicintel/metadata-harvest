# Scheduled harvesting

The **Scheduled harvest** workflow runs daily at **04:15 Europe/Stockholm** and
can be started manually from GitHub Actions. Set `HARVEST_ENABLED=false` to pause
automatic runs. `HARVEST_PARALLEL_PROVIDERS` defaults to 19. Provider configuration
is in `providers.json`; excluded providers must not be reintroduced.

## Current output only

Every adapter owns `data/<adapter>/`. The existing v1 job defaults to
`data/pxweb_v1/`; future jobs must use their own directory.

| Path within the adapter directory | Purpose |
|---|---|
| `metadata/` | One JSONL per provider/language, latest metadata only |
| `table-records/` | One JSONL per provider/language for current discovery state |
| `reports/refresh.json` | Latest listing result and completeness/errors; overwritten |
| `reports/harvest.json` | Latest processing result and counts; overwritten |
| `work/current.json` | Unfinished work only; removed when complete |

Dataset identity is provider + dataset code + language. Collection filenames
escape identifiers and add a checksum to avoid Windows case collisions. Each
JSONL line is one dataset document. Multiple known source locations are grouped
in `paths`, with the newest source timestamp determining the primary endpoint.

**Confirmed-absent datasets are deleted.** There is no inactive directory,
reactivation state, dated output collection or application-level version history.
If a removed dataset appears again, it must be fetched anew.

A timeout, missing worker or incomplete traversal does not prove deletion.
Those failures are reported and leave the previous metadata pending verification.
This prevents a temporary provider outage from emptying the published collection.
An upstream modification time is distinct from a harvest/report timestamp.

## Run lifecycle

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

## Development

Library functions accept the adapter's data directory. The existing Actions runner
uses `data/pxweb_v1/` by default; use a separate `--data` directory for experiments.
`uv run python -m metadata_harvest.runner prepare` prints the next phase without
writing. Existing commands remain available; no new CLI interface is required.

Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
Tests cover adapter separation, confirmed removal, incomplete discovery, failed
replacements, bounded status files, continuation, stable JSONL and safe publication.
See [the output contract](output-contract.md) and [source inventory](consolidation.md).

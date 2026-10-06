# Scheduled harvesting

The **Scheduled harvest** workflow runs daily at **04:15 Europe/Stockholm** and
can be started with **Actions → Scheduled harvest → Run workflow**. Leave the
continuation field empty. All permanent files are committed to `main`.

## Controls

- Schedule: edit `on.schedule` in `.github/workflows/harvest.yml`.
- Pause automatic runs: set repository Actions variable `HARVEST_ENABLED=false`.
  Manual runs still work. Set it to `true` or remove it to resume scheduling.
- Parallelism: `HARVEST_PARALLEL_PROVIDERS` defaults to `19`. Each provider has
  its own job; that job processes requests sequentially with provider pacing.
- Providers: edit root `providers.json`. Excluded-provider history is in
  `docs/excluded-providers.json`. Never add excluded entries to the active file.

GitHub schedules can start late; the schedule is not an exact-time guarantee.
The concurrency group prevents overlapping production workflows and does not
cancel a running harvest. Avoid running live diagnostic workflows against the
same providers while production is harvesting.

## Persistent files

| Folder | Contents |
|---|---|
| `data/table-records/` | Last valid grouped listing record for each dataset |
| `data/metadata/active/` | Completed, metadata-only JSON-stat2 datasets |
| `data/metadata/inactive/` | Completed datasets confirmed absent from refreshed listings |
| `data/reports/refresh/` | Permanent compact inventories and refresh errors |
| `data/reports/harvest/` | Per-run statistics, failures and completion status |
| `data/work/` | Current work list and continuation checkpoint |

Dataset identity is `(provider_code, dataset_code, language)`. Filenames contain
escaped identifiers plus an identity checksum, preventing collisions on Windows,
including identifiers differing only in case. Database/category paths never create
additional dataset files. `paths` retains each location's database, categories,
title, URLs and provider timestamp. The most recently updated location supplies
the primary endpoint; ties use ascending API URL. Timestamp precision is preserved.

## Run lifecycle

1. **Refresh.** A provider matrix reads every configured language/database.
   Individual malformed rows are logged while other rows survive. Each result
   includes observed identities and whether navigation completed. A collector
   combines locations, writes valid records and saves the compact inventory as
   `<UTC-datetime>_<successful-record-count>.json`. Each inventory row contains
   `provider_code`, `dataset_code`, `language`, `updated`. Completeness and errors
   are reported separately. These files and the work plan are committed before
   metadata jobs begin.
2. **Reconcile.** Missing metadata or a different timestamp queues a build,
   including corrected timestamps that moved backwards. Unchanged metadata is
   reused and its listing identity/paths refreshed. Confirmed missing datasets
   move to inactive; returning datasets move back to active and rebuild if stale.
   Failed or incomplete scopes cannot retire datasets or erase known paths. An
   unrefreshed primary location cannot be replaced by an older archive listing.
3. **Build.** Jobs process the work list by provider. One failed table does not
   interrupt its neighbors. The collector validates and saves successful datasets,
   leaves failed replacements untouched, and commits outcomes and the run report.

The last successfully written dataset's `updated` value is the processing state.
There is no separate timestamp advanced before a successful build. New failures
remain missing; failed updates retain their previous completed files. Both are
eligible on the next full refresh.

## Reports and recovery

Harvest reports count `reused`, `created`, `updated`, `retired`, `reactivated`,
`deferred`, `failed` and `unfinished`. Reactivated records can also count as reused
or updated, so those counts are not a partition. Status is `running`, `success`,
`completed_with_errors` or `interrupted`. Error entries include the dataset and
endpoint where available. Scope failures are linked through the refresh report.

Only collectors push; provider jobs exchange temporary Actions artifacts. Files
are replaced atomically. Publication rebases over unrelated commits with bounded
retries and never force-pushes. A publishing conflict stops the collector and
keeps its artifact for inspection instead of overwriting someone else's changes.

Workers stop accepting new tables before the GitHub job deadline, upload their
completed work and leave the rest pending. The collector commits the checkpoint
and dispatches a continuation using the same logical run ID. Continuations skip
refresh and process only pending tasks. Exhausted table failures are terminal for
that run, not endlessly retried. Three waves without progress stop automatic
continuation with `interrupted` status.

After interruption, use **Run workflow**, not GitHub's **Re-run failed jobs**:
the new invocation reads the latest checkpoint and resumes it. Scheduled starts
do this too, before beginning another refresh. Do not delete or hand-edit the work
list while a run is active. Permanent reports survive artifact expiration.

No external credentials are required: collector jobs use the repository-scoped
`GITHUB_TOKEN` to commit and dispatch continuations. Repository policy must allow
their declared `contents: write` and `actions: write` permissions.

## Development

`uv run python -m metadata_harvest.runner prepare` prints the next phase without
writing files. Other subcommands are `refresh`, `collect-refresh`, `build`,
`collect-metadata`, `publish`, and `continue`; use `--help` for paths and arguments.
Use a separate `--data` directory for local experiments. `publish` intentionally
stages only the repository's `data/` tree.

Run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
Tests cover partial listings, protected scopes, path grouping, retirement and
reactivation, failed replacements, continuation, atomic files, and publication
over an unrelated Git commit. The manual live compatibility workflows remain
available as diagnostic tools; production creates actual corpus files and reports.

# SCB PXWeb v2 metadata

The importable `metadata_harvest.adapters.pxweb_v2` package targets SCB's
production API, configured in `providers.pxweb_v2.json`, in Swedish and English.
It reads `/config`, all `/tables` pages, full table `/metadata`, and referenced
`/codelists`. It never calls observation or saved-query endpoints.

```python
from metadata_harvest.adapters.pxweb_v2 import (
    Client,
    discover_provider,
    fetch_metadata,
    fetch_codelist,
)
from metadata_harvest.adapters.pxweb_v2.validation import codelist_ids
from metadata_harvest.storage import read

provider = read("providers.pxweb_v2.json")[0]
async with Client(provider["base_api_url"]) as client:
    async for scope in discover_provider(provider, client=client):
        if not scope["complete"]:
            raise RuntimeError(scope["errors"])
        for record in scope["records"]:
            document = await fetch_metadata(record, client=client)
            # Production orchestration deduplicates these requests per language.
            for code in sorted(codelist_ids(document)):
                codelist = await fetch_codelist(code, record["language"], client=client)
```

## Output and ownership

`data/pxweb_v2/metadata/` contains one deterministic gzip-compressed JSONL collection per
language. Each line preserves SCB's native metadata-only JSON-stat2 response,
including title, source timestamp, category counts, source extensions and empty
`value`. `extension.metadata_harvest` adds `provider_code`, `dataset_code`,
`language`, `base_api_url`, and the full `catalogue` record from discovery.
It does not replace native SCB fields or claim external schema conformance.

`data/pxweb_v2/codelists/` contains one `.jsonl.gz` collection per language. Each line
has `provider_code`, `language`, and `codelist`, the complete native codelist
response. Its source ID resolves the references in dimension extensions.
Codelists are stored once per language, never disguised as statistical datasets.
The existing collision-safe provider/language filenames are reused in both
directories with `.jsonl.gz` extensions. Gzip timestamps and filenames are fixed
so unchanged content produces identical compressed bytes; decompression returns
complete UTF-8 JSONL without truncation. There are no per-table or per-codelist result files.

`table-records/` holds the latest catalogue state, also grouped by language.
`reports/refresh.json` and `reports/harvest.json` overwrite their previous values.
`work/current.json` exists only while work remains. Reports include current
discovery evidence, failures, full/incremental mode and counts; source dates stay
separate from run timestamps.

## Refresh and source removals

Every new run discovers both complete language catalogues independently, including
discontinued tables that remain accessible. Pagination always sends explicit
language, page and `includeDiscontinued=true` parameters. Incomplete discovery
cannot prove absence. Different language table counts are expected.

Incremental runs compare each current catalogue record with the last successfully
accepted record. New, changed, invalid and previously failed records are fetched.
Their referenced codelists are refreshed, once per language per run; missing
codelists are fetched too. Full runs fetch every table and referenced codelist,
catching upstream corrections that do not change catalogue timestamps. Source
updates during retrieval receive one catalogue/metadata retry.

Confirmed absent tables are deleted. Two 404/410 responses also confirm a table
or codelist has become inaccessible during a run. Removed codelist payloads are
deleted; valid table metadata is accepted independently. Codelist errors are
reported separately with `stage: codelist` and `counts.codelists_failed`; they
never increment the failed-table count or block new/updated table metadata.
Later incremental runs retry unresolved or previously failed codelists without
refetching unchanged table metadata. The overall run remains explicitly incomplete
while dependency errors remain. Unreferenced
codelists are pruned. Transient failures preserve prior accepted content without
advancing its source timestamp. No inactive archive, dated snapshot, version
history or reactivation store is produced.

## Execution and automation

```powershell
uv run python -m metadata_harvest.runner run --adapter pxweb_v2 --full
uv run python -m metadata_harvest.runner collect --adapter pxweb_v2
```

The worker reads published data and writes a candidate under `output/pxweb_v2/`.
Use an empty output directory for each invocation (`--output` changes it).
`--data` selects a different adapter root for isolated local verification.
The collector validates content, identities, reference resolution, removal
evidence, checkpoint status and the original input fingerprint before changing
the selected data root. Git publication stages only that adapter's directory.

One worker owns both languages and a shared HTTP limiter (at least 0.5 seconds
between requests, slower if `/config` requires it). Requests have 60-second
timeouts and at most three retries for transport failures, 429 and server errors.
Retry-After delays exceeding 120 seconds stop that request instead of retrying
too soon. A source race cannot become accepted metadata. A dependency failure does not
invalidate valid table metadata; its separate error remains visible.

Work is flushed in batches, every 100 processed tables or 60 seconds. Temporary
worker buffers keep the last certified batch intact while writing its replacement;
an atomic result marker selects the complete batch, and the previous buffer is
then discarded. They are bounded transport files, never published metadata history.
Unfinished checkpoints preserve full mode and successful progress across workflow
continuations. Three waves with no progress stop automatic continuation.

The independent `SCB PXWeb v2 metadata` workflow runs at 06:15 Europe/Stockholm:
incremental Monday–Saturday, full Sunday. Manual runs expose a `full` boolean.
Scheduling requires `PXWEB_V2_ENABLED=true`, set only after live acceptance.
Both adapters share production workflow concurrency; collectors alone publish.
Pending runs use GitHub's `queue: max` so v1 continuations cannot displace a
queued v2 run (the default concurrency queue retains only one pending run).
Artifacts expire after seven days. Collection rejects files reaching GitHub's
100 MiB limit before publication; it never truncates or silently splits output.

## Verification

Deterministic HTTP fixtures and filesystem tests cover pagination failures,
language separation, complete categories, codelist validation, shared references,
incremental/full refresh, confirmed deletion, transient failures, continuation,
crash-safe batches, stale artifacts, scoped Git publication and unchanged bytes.

On 8 October 2026, bounded live discovery validated 5,278 Swedish and 3,342
English catalogue records. Live metadata for TAB638 validated all six dimensions
and all 22 referenced codelists in each language. Complete automated harvesting
completed on main in [run 37815480019](https://github.com/nordicintel/metadata-harvest/actions/runs/37815480019):
8,620 tables discovered, 8,537 initially accepted and 83 blocked by codelist
responses (404/500). That coupling was corrected: valid tables now survive
codelist failures. Plain metadata measured 145.85 MiB (sv) and 116.25 MiB (en),
so publication stopped before Git staging. The approved format is now `.jsonl.gz`.
Final compressed publication and incremental verification remain in progress;
scheduling stays disabled until acceptance.

# SCB PXWeb v2 metadata

The importable `metadata_harvest.adapters.pxweb_v2` package targets SCB's
production API, configured in `catalogs.json`, in Swedish and English.
It reads `/config`, all `/tables` pages, full table `/metadata`, and referenced
`/codelists`. Harvesting never calls observation or saved-query endpoints; the separate retrieval module calls the data endpoint only on demand.

```python
from metadata_harvest.adapters.pxweb_v2 import (
    Client,
    discover_provider,
    fetch_metadata,
)
from metadata_harvest.configuration import providers_for_adapter

provider = providers_for_adapter("pxweb_v2")[0]
async with Client(provider["base_api_url"]) as client:
    async for scope in discover_provider(provider, client=client):
        if not scope["complete"]:
            raise RuntimeError(scope["errors"])
        for record in scope["records"]:
            contributions = await fetch_metadata(record, client=client)
            # Validated HarvestContribution instances for shared assembly.
            for contribution in contributions:
                dataset = contribution.dataset
```

## Output and ownership

`data/pxweb_v2/metadata/` contains one deterministic multilingual collection per
canonical provider, named `<owner>__multilingual__<digest>.jsonl.gz`. Each line is a
validated StatDCAT dataset with language-tagged metadata and independent retrieval
distributions. The owner is SCB; its access service is the configured SCB v2 API, backed by the shared `pxweb_v2`
adapter definition. SCB's other API services have their own stable identities. Source identity and operational options live in the retrieval binding;
standard properties and source extensions preserve the remaining metadata.
See the [profile and field mapping](output-contract.md).

Public `fetch_codelist(code, language, client=...)` returns a typed SKOS scheme.
Production workers deduplicate native codelist requests per language before
publication; native caches below are distinct from the final `codelists.7z` output.

`data/pxweb_v2/codelists/` contains one `.jsonl.gz` collection per language. Each line
has `provider_code`, `language`, and `codelist`, the complete native codelist
response. Its source ID resolves the references in dimension extensions.
Codelists are stored once per language, never disguised as statistical datasets.
Native codelist cache filenames retain provider/language grouping; metadata uses
canonical-provider multilingual grouping. Gzip timestamps and filenames are fixed
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
uv run --extra harvest python -m metadata_harvest.runner run --adapter pxweb_v2 --full
uv run --extra harvest python -m metadata_harvest.runner collect --adapter pxweb_v2
```

The worker reads published data and writes a candidate under `output/pxweb_v2/`.
Use an empty output directory for each invocation (`--output` changes it).
`--data` selects a different adapter root for isolated local verification.
The collector validates content, identities, reference resolution, removal
evidence, checkpoint status and the original input fingerprint before changing
the selected data root. Git publication stages only that adapter's directory.

One worker owns both languages and a shared HTTP limiter. After `/config`, request
starts are spaced by `timeWindow / maxCallsPerTimeWindow`: SCB's 30 calls per
10 seconds permits about 0.333 seconds, without an additional 0.5-second floor.
The initial config request uses a conservative bootstrap interval. Stricter
published limits and Retry-After still apply. Requests have 60-second
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

The independent `SCB PXWeb v2 metadata` workflow is configured for 06:15 Europe/Stockholm:
incremental Monday–Saturday, full Sunday. Manual runs expose a `full` boolean.
Scheduling requires enablement in [schedules.toml](../schedules.toml), set only after live acceptance.
Adapters harvest concurrently; only collectors share `production-publication`.
Each adapter queues its own overlapping runs with `queue: max`. The live
[progress and error inspector](harvesting.md#inspect-a-running-job) reports actual
outcomes during the run; codelist errors remain separate from table failures.
Artifacts expire after seven days. Collection rejects files reaching GitHub's
100 MiB limit before publication; it never truncates or silently splits output.

## Verification

Deterministic HTTP fixtures and filesystem tests cover pagination failures,
language separation, complete categories, codelist validation, shared references,
incremental/full refresh, confirmed deletion, transient failures, continuation,
crash-safe batches, stale artifacts, scoped Git publication and unchanged bytes.

On 8 October 2026, full production discovery and the corrected automated
[harvest](https://github.com/nordicintel/metadata-harvest/actions/runs/37832796927)
accounted for **5,278 Swedish and 3,342 English tables**, with zero table failures.
Every accepted record passed native metadata validation with `value: []`.
All 2,011 referenced language-specific codelists are accounted for: 1,956 accepted
(1,057 sv, 899 en) and 55 separately reported upstream failures (21 sv, 34 en;
13 HTTP 404 responses and 42 HTTP 500 responses). No valid table is rejected
because a referenced codelist fails.

| Collection | Records | Compressed MiB |
| --- | ---: | ---: |
| Swedish metadata | 5,278 | 24.63 |
| English metadata | 3,342 | 21.05 |
| Swedish codelists | 1,057 | 3.87 |
| English codelists | 899 | 2.93 |

The [initial full run](https://github.com/nordicintel/metadata-harvest/actions/runs/37815480019)
correctly stopped publication because plain metadata exceeded GitHub's 100 MiB
limit. The approved `.jsonl.gz` conversion verified decompressed SHA-256 against
the original JSONL; complete collections were then validated and published.
No source fields or records were removed to reduce file sizes.

The [subsequent incremental run](https://github.com/nordicintel/metadata-harvest/actions/runs/37833319611)
reused all 8,620 tables with no created, updated or failed tables. All four
compressed collections remained byte-identical (SHA-256 checked); only the latest
reports changed. Collection and publication succeeded; the workflow reports
incomplete harvesting because the same 55 upstream codelist errors remain.
At that v2 verification point, 170 deterministic tests passed. After the v1 provider
additions, the suite passed 175 tests and [CI](https://github.com/nordicintel/metadata-harvest/actions/runs/37844771664)
passed lint, formatting and tests; local package build also passed.
That evidence predates the shared-schema mapping. The agreed scheduling gate is
now complete table acceptance, even with independently reported codelist errors.
Canonical conversion, standalone retrieval and renewed production acceptance are
tracked in [consolidation](consolidation.md).

# PXWeb v1 discovery and metadata

The importable `harvesting.adapters.pxweb_v1` package uses website discovery
and API metadata retrieval. The top-level imports remain supported. Configuration
is in [`catalogs.json`](../catalogs.json); see [provider additions and live evidence](pxweb-registry-gaps.md).

Public `fetch_metadata` returns typed `HarvestContribution` models for shared
assembly. Accepted collections combine languages per canonical owner; original
routing aliases and retrieval settings remain distribution-specific. Concrete DataServices reference the shared `pxweb_v1` adapter definition. The SCB main-v1 publication
filter does not remove SCB HVD or other providers. See the [metadata profile](output-contract.md)
and [owner inventory](source-inventory.md). Discovery dictionaries below remain
native operational records.

```python
from harvesting import get_pxweb_tables

records = await get_pxweb_tables(
    "https://example.org/PXWeb/pxweb/",
    languages=["sv", "en"],
    database_ids=["Statistics"],
)
```

`languages=None` tries English and Swedish. A language in the URL overrides that
list. A database in the URL is combined with supplied database IDs. Without a URL
database, `database_ids=None` discovers databases separately for each language;
`database_ids=[]` selects none. Folder and table inputs harvest their entire database.
API-form inputs are converted to web URLs before navigation.

The function returns dictionaries with `id`, `path`, `category_path`, `title`,
`score` (always `1.0`), `published`, `api_url`, `web_url`, `language`, and
`database_id`. It preserves actual table links, meaningful category numbering,
and date precision. Website listing dates are retained. Missing dates or titles
use one flat API search per language/database, joined by full path and table ID.
For dates still missing, only the corresponding API parent folders are requested,
with a cached result per folder. API-only tables are never added and API subfolders are never crawled.
Tables still undated are omitted with diagnostics containing their IDs and paths.
Linköping may use category IDs as labels where the website supplies none.
Root-level paths are `/`.

One Chromium instance is used per invocation, with isolated contexts. Browser API
traffic is blocked before navigation using context routing and Chromium network
blocking (including redirects), with service workers disabled. Only the explicit
metadata enrichment requests above use Playwright's request client. Unsolicited
popups and embedded frames are blocked. The discovery function does not visit individual
table pages or write output files. The separate runner orchestrates publication.

Unsupported combinations require explicit website evidence and produce logging
diagnostics. Automatic discovery skips candidates with no usable listing or missing
titles/category labels, logging the database and reason. Explicitly requested
databases still fail on those defects. Timeouts, server errors and incomplete
traversal remain failures in both modes, aggregated into an `ExceptionGroup` after
the remaining combinations are attempted.
Transient web failures are retried at most four times, respecting `Retry-After`.
Flat API enrichment is attempted once. Parent-folder lookups are cached and
retry transient failures up to four times, respecting `Retry-After`. Exhausted
request failures remain errors rather than being treated as absent dates.
Failed stateful expansion restarts from a fresh navigation. Certificate failures
retry in a new context using `ignore_https_errors=True`, with a warning.

## Complete table metadata

```python
from harvesting import get_pxweb_metadata

dataset = await get_pxweb_metadata(records[0], provider_code="provider-code")
```

This operation uses the record's complete API URL for GET metadata and JSON-stat2
POST slices. It recovers missing category lists, then covers every category with
bounded slices (at most 10,000 cells, or the provider's lower advertised limit).
It returns full dimensions with `value=[]`. No browser, files or persistent state
are involved. Requests are sequential and paced by provider configuration; callers
must also coordinate concurrent operations against the same provider.

Metadata titles come from accepted POST metadata, then GET, then the listing as
fallback. Shared identity is in `extension.nordicintel`; full source listing and
native extras are retained under `extension.pxweb_v1`. Units, notes, links and
extensions pass the [common mapping and semantic validation](output-contract.md).
Observation values are discarded; sampled flag meaning and synthetic eliminated
dimensions remain namespaced metadata. Category coverage does not mean every
combination of observations was downloaded. Root `updated` is a calendar date,
with original source precision retained for incremental comparison.

Standalone observation retrieval is in `retrieval.adapters.pxweb_v1`
and uses the caller's aiohttp session; see the [retrieval guide](retrieval-guide.md).

Run **Live PXWeb metadata** manually in GitHub Actions for every web-listed table
across all configured v1 language/database scopes. It uploads per-table request counts, category
coverage, timestamps, hashes and failures. Local smoke checks can use
`uv run --extra harvest python scripts/check_metadata_live.py --provider mi --output output/mi-metadata.json --limit 1`;
limited runs deliberately never count as full acceptance. A failed GET, POST,
recovery or completeness check raises an error rather than returning a partial
dataset. Store/rebuild successfully before advancing the processed `published` value.

## Development and validation

Run **Live PXWeb compatibility** from GitHub Actions (`workflow_dispatch`) to
test all configured v1 providers and scopes on GitHub-hosted Chromium. Provider jobs
run independently (maximum four at once); per-provider JSON evidence and a
complete-matrix summary are uploaded even when a check fails. Hidden databases
are tested explicitly and reported separately from discovery comparisons.

```powershell
uv sync --locked --extra harvest
uv run --extra harvest playwright install chromium
uv run --extra harvest ruff format --check .
uv run --extra harvest ruff check .
uv run --extra harvest pytest
```

On Linux, install browser system dependencies with
`uv run --extra harvest playwright install --with-deps chromium`. CI runs only deterministic
local browser tests, including a test-only self-signed HTTPS fixture and server-side
counters proving that unsolicited direct and redirected browser API requests never
reach the server, while explicitly authorized enrichment requests are counted.
The fixture key is public test data and must never be used for a real service.

The active provider configuration is in `catalogs.json`:
26 providers, 52 databases, 62 language/database combinations. Run live acceptance
explicitly (it can take several minutes):

```powershell
uv run --extra harvest python scripts/check_pxweb_live.py
# Optional focused rerun:
uv run --extra harvest python scripts/check_pxweb_live.py --provider linkoping --output output/linkoping.json
# Resume an interrupted run without discarding successful checks:
uv run --extra harvest python scripts/check_pxweb_live.py --resume
```

The runner writes per-combination counts, errors, exact API/web input comparisons,
discovery differences, timestamps, and successful records to `output/compatibility.json`.
The runner preserves actual URLs and reports UUID-valued `rxid` session differences
separately from semantic equivalence. It also checks one rendered table page per
successful combination and records its title, categories, route, and metadata date.
It runs at most three providers concurrently; each provider navigates sequentially.
Use `--timeout` to change the default 900-second limit per invocation.
The command exits nonzero if compatibility acceptance fails.
Live service failures and source metadata gaps are not passing acceptance results.
See [the dated compatibility report](pxweb-compatibility.md) for configured
harvest results and the separately reported discovery limitations.


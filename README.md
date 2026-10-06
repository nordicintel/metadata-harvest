# metadata-harvest

Web-authoritative PXWeb table discovery and metadata listings for Python 3.13+.

```python
from metadata_harvest import get_pxweb_tables

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
popups and embedded frames are blocked. The library does not visit individual
table pages, write output files, or expose a CLI.

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

## Development and validation

Run **Live PXWeb compatibility** from GitHub Actions (`workflow_dispatch`) to
test all 20 providers / 55 combinations on GitHub-hosted Chromium. Provider jobs
run independently (maximum four at once); per-provider JSON evidence and a
complete-matrix summary are uploaded even when a check fails. Hidden databases
are tested explicitly and reported separately from discovery comparisons.

```powershell
uv sync --locked
uv run playwright install chromium
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

On Linux, install browser system dependencies with
`uv run playwright install --with-deps chromium`. CI runs only deterministic
local browser tests, including a test-only self-signed HTTPS fixture and server-side
counters proving that unsolicited direct and redirected browser API requests never
reach the server, while explicitly authorized enrichment requests are counted.
The fixture key is public test data and must never be used for a real service.

The supplied provider configuration is preserved in `tests/fixtures/providers.json`:
20 providers, 45 databases, 55 language/database combinations. Run live acceptance
explicitly (it can take several minutes):

```powershell
uv run python scripts/check_pxweb_live.py
# Optional focused rerun:
uv run python scripts/check_pxweb_live.py --provider linkoping --output output/linkoping.json
# Resume an interrupted run without discarding successful checks:
uv run python scripts/check_pxweb_live.py --resume
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
See [the dated compatibility report](docs/pxweb-compatibility.md) for configured
harvest results and the separately reported discovery limitations.

## Scheduled integration boundary

The future daily GitHub Actions wrapper should call this function, compare
`published` with the last successfully processed value for each
`(language, database_id, path, id)`, and rebuild metadata for new or changed tables.
Advance state only after rebuilding succeeds. Treat extraction exceptions as failed
jobs, never as empty catalogs. Provider jobs may run independently in parallel.
Scheduling, persistent state, and metadata rebuilding are outside this module.

No PyPI publication or license has been configured.

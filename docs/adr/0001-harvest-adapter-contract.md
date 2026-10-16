# ADR 0001: One harvest lifecycle, one adapter contract

Status: proposed (2026-10-16)

## In one paragraph

Today every adapter folder under `apps/harvest/metadata_adapters/` has its own
400 to 1100 line `workflow.py` that repeats the same harvest loop with different
nouns. This ADR moves that loop into one shared module, `apps/harvest/lifecycle.py`,
and fixes exactly which files and functions an adapter folder must contain. An
adapter becomes three small files: how to talk to the provider (`client.py`), how to
find and fetch things (`harvest.py`), and how to turn stored responses into datasets
(`parse.py`). Everything else is shared and written once.

## Words used in this document

- **Adapter**: one provider integration, one folder under
  `apps/harvest/metadata_adapters/`. Examples: `sdb`, `trafa`, `kolada`.
- **Harvest**: fetch responses from a provider's API and store them as files, as
  received, under `data/<adapter>/source/`. No interpretation happens here.
- **Provider**: a `ServiceBinding` from the registry (see ADR 0003): one configured
  endpoint for one adapter, with `source_code`, `endpoint_url`, publisher and options.
  Amended 2026-10-16: adapters receive the typed binding, not a dict.
- **Parse**: read those stored files, offline, and turn them into StatDCAT dataset
  documents. Happens later, by hand, never inside a harvest run.
- **Lifecycle**: the shared loop around a harvest run: copy the previous files, ask
  the adapter what exists, fetch each item, retry failures once, decide what is
  published or withheld, write state and reports, and apply the result back.
- **Key**: the identity of one stored item, written `<language>/<code>`, for example
  `sv/AMN`. One key is one source file.
- **Scope**: a group of keys that is listed together and fails together. For SDB a
  scope is a language. For Trafikanalys a scope is a product with many datasets.
- **Published / withheld**: a stored item is published when its endpoint answered in
  this run and it was verified recently; otherwise it is withheld. Withheld items
  keep their files; parsing skips them. This rule lives in `apps/harvest/currency.py`
  and is not adapter-specific.
- **Seam**: a line in the code where behaviour on one side can be replaced without
  editing the other side. The parse seam already exists and works; the harvest seam
  is what this ADR creates.

## The problem

Twelve adapters, twelve copies of the same loop. Reading `sdb/workflow.py` and
`trafa/workflow.py` side by side, roughly 350 of 400 lines are identical except for
log text. The `collect()` function that applies a run's output is byte-identical in
nine files. Each adapter also has its own `state.py` that differs only in the name of
one dictionary (`subjects` versus `datasets`).

Because there is no declared interface, knowledge about adapters has leaked into the
callers:

- `apps/operations/harvest.py` holds a table (`SIMPLE_ADAPTERS`) that knows each
  adapter's `run()` keyword arguments.
- `apps/operations/outcome.py` holds a twelve-branch `if adapter == ...` ladder that
  decides what a failed scope means per adapter.
- `apps/harvest/registry.py` lists adapters but offers nothing a caller can invoke.
- Every adapter has its own `test_workflow.py` that stubs the whole loop again.

Adding an adapter today means copying a 600-line file and editing it in forty places.

## The decision

### Three parts, three responsibilities

```text
apps/harvest/lifecycle.py        the shared loop (written once)
apps/harvest/metadata_adapters/  one folder per provider (the only code per adapter)
apps/parsing/build.py            the shared parse loop (already exists, unchanged)
```

The lifecycle calls the adapter. The adapter never calls the lifecycle, never writes
files itself, never sleeps, never retries, and never decides published or withheld.

### What an adapter folder contains

Exactly these files. Nothing else is required; nothing else is imported from outside
the folder.

```text
apps/harvest/metadata_adapters/<name>/
├── __init__.py      empty
├── client.py        HTTP calls to the provider; URLs, parameters, response decoding
├── harvest.py       ADAPTER: how to discover scopes and fetch one task
├── parse.py         documents() and withheld_keys(): stored files -> datasets
└── mapping.py       optional: pure functions shared by harvest.py and parse.py
```

Adapters may add more private modules (`probe.py`, `regions.py`, `categories.py`)
for their own use. They are implementation, not interface: nothing outside the folder
imports them.

Files that disappear from every adapter: `workflow.py`, `state.py`, `source.py`,
`api.py` (fold into `client.py` or `mapping.py`).

#### `client.py`

One class. Knows the provider's URL shapes and how to decode responses. Raises
`lifecycle.Unreachable` when the provider does not answer, and nothing else that the
lifecycle has to know about.

```python
class Client:
    def __init__(self, provider: ServiceBinding): ...
    async def get(self, *parts, **params): ...   # whatever the provider needs
    async def close(self) -> None: ...
```

Tests inject a fake client. The lifecycle closes the client only if it built it.

#### `harvest.py`

One object named `ADAPTER`. This is the whole harvest interface of an adapter.

```python
from apps.harvest import lifecycle as lc

class Sdb:
    name = "sdb"                      # folder name, data/<name>, SourceStore adapter
    provider_code = "socialstyrelsen" # first segment of every stored key
    collection = "subjects"           # name of the per-key dictionary in state
    counts = ("rebuilt", "unchanged", "failed", "withheld", "restored", "removed")
    options = ("subjects", "languages")   # CLI selection arguments this adapter accepts
    default_languages = ("sv", "en")

    def client(self, provider: ServiceBinding) -> Client: ...

    async def discover(self, run: lc.Run) -> AsyncIterator[lc.Scope]:
        """Yield each scope with the tasks it lists. Raising aborts the run."""

    async def fetch(self, run: lc.Run, task: lc.Task) -> lc.Fetched | lc.Kept | lc.Drop:
        """Fetch one task. Raise Unreachable if the endpoint did not answer."""

    def forget(self, key: str) -> list[str]:
        """Shared files to delete when this key is removed and no sibling uses them."""

    def scope_failures(self, report: dict) -> list[str]:
        """Which whole scopes failed, read from the report. Used by outcome.py."""

ADAPTER = Sdb()
```

What each return value means:

| Returned from `fetch` | What the lifecycle does |
| --- | --- |
| `Fetched(responses, shared=..., derived=...)` | Stores the envelope. Counts `unchanged` if identical to the stored one, else `rebuilt`. Marks the key verified. |
| `Kept(count="reused")` | Endpoint checked, stored entry left as is. Counts under the given name. |
| `Drop(count="untranslated")` | Key leaves the store and state entirely. |
| raises `Unreachable` | Endpoint failure. Key marked not working. Retried once after the cooldown. |
| raises `Rejected` (from the store) or any other exception | Metadata failure. The endpoint answered, so the stored entry is kept. Not retried. |

What `Scope` carries:

```python
@dataclass
class Scope:
    name: str                        # "sv", "T10011", "catalogue"; used in error resources
    expand: Callable[[Run], Awaitable[Listing]]   # one listing request
    required: bool = False           # failure aborts the whole run (SDB's Swedish listing)

@dataclass
class Listing:
    tasks: list[Task]                # what to fetch in this scope
    complete: bool                   # True: keys not listed may be removed
    covers: Callable[[str], bool]    # which known keys belong to this scope
    report: dict = {}                # extra report fields (Trafikanalys exclusions)
```

The `Run` object handed to `discover` and `fetch` is read-mostly: it exposes
`client`, `provider`, `selection`, `languages`, `stamp`, a `share(name, body)` to
store a shared file, `stored(key)` to read a stored envelope, and `fail(stage,
resource, error)` to record a non-fatal problem. It does not expose the state
dictionary or the store's write methods.

#### `parse.py`

Unchanged from today. Two functions, called by `apps/parsing/build.py`:

```python
def withheld_keys(state: dict) -> set[str]: ...
def documents(store: SourceStore, state: dict, providers: list[ServiceBinding], *, portal=None)
    -> Iterator[tuple[str, dict | Exception]]: ...
```

Each document names its identity as `source_code`, dataset code and language. The
registry mints the stable ID from those with one rule for every adapter (ADR 0003);
`parse.py` has no identity hook.

### What the lifecycle owns

Written once in `apps/harvest/lifecycle.py`, in this fixed order. An adapter cannot
change the order or skip a step.

1. Check that `data` and `output` are separate directories.
2. Check the previous files are only `source/`, `state/current.jsonl.gz` and
   `reports/latest.json`. Copy them into `output`.
3. Open the `SourceStore`. Load state. Rebuild the published and withheld sets from
   the store and state.
4. Validate the selected languages against the provider's configured ones.
5. Call `discover()`. For each scope: run `expand()`, record listing problems, and if
   the listing is complete and the run is unrestricted, remove known keys that were
   not listed (and their shared files via `forget()`).
6. Fetch every task. Collect the tasks that raised `Unreachable`.
7. Sleep `retry_cooldown_seconds` once, then fetch the collected tasks a second time.
8. Record remaining failures, sorted by key, into the report and into each key's state.
9. Run `settle()` from `apps/harvest/currency.py` over every key touched this run.
10. Save the store, save state, write `reports/latest.json`, check sizes, write
    `result.json` with the manifest.

And one more function, `collect(adapter, data, output)`, that applies a finished
output back into `data` after checking the baseline did not change. It is the same
for every adapter.

The result dictionary always has `run_id`, `status`, `continue`, `selection`,
`counts`, `published`, `withheld`, `errors` in that order, because GitHub workflows
and `outcome.py` read them.

### What the callers lose

- `apps/operations/harvest.py`: the `SIMPLE_ADAPTERS` table and the per-adapter
  branches. It loads `ADAPTER` from the folder, reads `ADAPTER.options` to know which
  CLI arguments to forward, and calls `lifecycle.run` or `lifecycle.collect`.
- `apps/operations/outcome.py`: the adapter ladder. It calls
  `ADAPTER.scope_failures(report)`.
- `apps/harvest/registry.py`: shrinks to the list of names and workflow files.
- `tests/harvest/<adapter>/test_workflow.py`: the lifecycle assertions (retry,
  removal, withheld and restored, stale artifact, byte-identical rerun) move to one
  `tests/harvest/test_lifecycle.py` with a scripted toy adapter. Each adapter keeps a
  `discover` test, a `fetch` test, and one end-to-end run that also calls its
  `parse.documents` so the harvest-to-parse coupling stays tested.

### The three irregular adapters

Not every adapter fits the fetch-and-settle loop. Being explicit about which do not:

- **kolada** reads a change feed rather than a listing, carries pending work from one
  run to the next, has a request budget, and never withholds. It keeps its own loop
  but uses the lifecycle's `Workspace` helper for steps 1 to 3 and 10, and the shared
  `collect()`. Its folder has `harvest.py` exposing `run()` and `collect()` plus
  `scope_failures()` instead of `ADAPTER`.
- **entryscape** harvests many providers in one run, stops the whole run when a host
  blocks it, and tracks tables in a second state file. Same treatment as kolada.
- **pxweb_v1** has a different command set (`prepare`, `refresh`, `build`,
  `collect-metadata`, `continue`), different report paths and worker checkpoints. It is
  left exactly as it is. It is the oldest adapter and `pxweb_v2` is its successor.

`Workspace` is the lower half of the lifecycle: the part that touches disk. Having
two adapters use it directly, next to the nine that use the full loop, is what makes
it a real seam rather than a hypothetical one.

### The seam between harvesting and parsing

Already in place since the 2026-10-16 cutover; this ADR only states it strictly.

- Harvesting writes `data/<adapter>/source/**`, `manifest.json`,
  `state/current.jsonl.gz` and `reports/latest.json`. Nothing else.
- Parsing reads those through `SourceStore` and the adapter's `parse.py`. It writes
  accepted collections elsewhere. It never fetches.
- `harvest.py` never imports `parse.py`, and `parse.py` never imports `harvest.py`.
  Shared pure helpers go in `mapping.py`.
- Everything downstream (catalog, portal, publication, migrations) reads accepted
  collections, never source files or adapter modules. The live check scripts under
  `scripts/live/` may import `client.py` and `ADAPTER`; nothing else outside the
  folder may import from an adapter.

## Consequences

Good:

- A new adapter is `client.py`, `harvest.py` and `parse.py`, around 200 lines total.
  Retry, removal safety, settle, reports, collect and the CLI come for free.
- One fix to the loop lands in nine adapters.
- `outcome.py` and `harvest.py` contain no adapter names.
- Lifecycle behaviour is tested once with a toy adapter instead of nine times with
  nine fakes.

Costs and risks:

- This is a byte-contract refactor. Source files, manifests, state and reports for
  successful runs must come out identical. Each adapter is ported with a golden diff:
  run old and new on the same fixtures and compare the output trees byte for byte.
- `reports/latest.json` is written without sorted keys, so field order is part of
  the contract. Adapters that add fields (Trafikanalys, UKÄ) do so through
  `Listing.report`, which the lifecycle splices before `errors`. Normalising report
  order is a separate follow-up.
- Two behaviours differ between adapters today and the lifecycle picks one. Both
  only affect reports of runs with failures, never source files: a failed scope writes
  its error into every covered key's state (Trafikanalys does, SDB does not; the
  lifecycle does), and retries happen per task rather than per product.
- Per-adapter log lines become generic. Files do not change; operator log text does.

## Not in this ADR

- Injecting the clock and HTTP transport as ports, with one shared retry, pacing and
  budget policy. Worth doing after this lands, starting from `pxweb_v2` whose client
  already takes a clock. Kept separate so the byte-contract refactor stays small.
- Normalising report key order.
- Touching `retrieval/`.

## Porting order

1. Write `lifecycle.py` and `test_lifecycle.py` with a toy adapter.
2. Port `sdb` (flat listing). Golden diff. Delete its `workflow.py` and `state.py`.
3. `smhi_emissions`, `riksbank`, `tillvaxtverket`, `dataanalys`, `forsakringskassan`
   in that order; each is a flat or two-level listing.
4. `uka` (needs `Kept`) and `trafa` (needs grouped scopes and `Drop`).
5. Move `kolada` and `entryscape` onto `Workspace` and shared `collect()`.
6. Delete `SIMPLE_ADAPTERS`, the `outcome.py` ladder, and the dead `languages`
   argument of `collect()`.

# Kolada

## Metadata contract

Public `fetch_metadata` returns typed `HarvestContribution` models. Accepted metadata
uses provider-grouped multilingual StatDCAT datasets; distribution retrieval bindings
retain the original source alias, language and scope. Each access service identifies a concrete API and references its adapter; publisher
relationships are resolved separately for catalogs, services and datasets. See the [profile and field
mapping](../../model/output-contract.md), [owner inventory](../../configuration/inventory.md) and
[typed retrieval](../../retrieval/README.md).

The native field names, JSON-stat examples and provider entry-point calls below
describe adapter parsing and worker representation and explicit historical readers, not the published JSON-LD
shape. Dated acceptance sections record their original verification.

The self-contained `apps.harvest.metadata_adapters.kolada` package implements Swedish
metadata and on-demand retrieval against [Kolada API v3](https://api.kolada.se/v3/docs).
There is no runtime dependency on another repository, database or data.eu.

## Output

`data/kolada/metadata/<owner>__multilingual__<digest>.jsonl.gz` groups by native KPI under owner `rka`. Municipality and OU are separate
distributions of that KPI; the routing alias remains `kolada`. Each retains its
original source identity, dimensions, coverage and update/failure state.
Municipality scope includes municipalities, regions, the national aggregate and the
44 configured groups. OU scope retains municipality relationships without a geo role.

Dimensions are year, municipality/OU and gender where male/female rows exist,
including suppressed rows. Total-only datasets omit gender and retrieve T values.
Published metadata contains no observations; the legacy native projection uses
`value: []` and no observation `status`.
Native KPI fields, publication details, group memberships and OU parents are retained.
The complete current reference catalogues live once in `state/current.jsonl.gz`;
dataset documents retain relevant labels and member IDs, not unrelated members of
entire KPI groups. `updated` is a verified native change day, never a harvest/portal date.

## Refresh and failures

```powershell
uv run --extra harvest python -m apps.operations.harvest run --adapter kolada
uv run --extra harvest python -m apps.operations.harvest collect --adapter kolada
# Restrict per-KPI work; small native reference catalogues still refresh.
uv run --extra harvest python -m apps.operations.harvest run --adapter kolada --kpi N78110 U30750
uv run --extra harvest python -m apps.operations.harvest run --adapter kolada --kpi U30750 --scope ou
```

Initial current data was imported once from trusted existing results. There is no
shipped seeding command, migration dependency or full-rescrape mode. No corpus-wide
validation or live recheck is required: validate a bounded sample and changed replacements.

Every refresh checks native listings and change signals first, then processes new,
changed or failed KPI scopes. Metadata-only changes reuse saved categories. Data
changes fetch that KPI/scope's current rows to rebuild coverage and immediately
discard values. Native change checks use the working OU path endpoint, not its
query-form filter. Inclusive one-day overlap protects same-day updates; an overlap
hit can require another targeted fetch despite an unchanged calendar date because
the API supplies no finer revision identifier.

Municipality periods start in 1970, OU in 1994, through the current year plus five.
The shared request interval is 0.225 seconds, with at most four active connections;
pages remain 5,000 for listings/coverage and 1,000 for compact change probes.
Transient change requests split into smaller batches with bounded retries.

Failures retain prior accepted documents independently per KPI/scope. Malformed
reference catalogues retain their previous state. Incomplete discovery cannot
authorize deletion; absent KPIs receive a direct metadata check. Complete coverage
with no live rows removes that scope. Discontinued but accessible data stays included.
No inactive store or observation database exists.

Completed change checks and the selected KPI/scope restriction survive continuations.
An ordinary daily/manual run starts fresh change checks and retains pending failures;
it never inherits a previous sample restriction. Only an explicit continuation reuses
the prior run's completed checks. A persistently failing scope cannot suppress updates
to other scopes on later days.
Pending work is saved with its check marker; accepted markers advance only with
successful replacements. Collectors check artifact hashes and the current baseline,
not every dataset. Unchanged metadata is reused without recompression. Only
`data/kolada/` is staged; latest reports and individual errors remain inspectable.

The independent workflow runs daily at 06:30 Europe/Stockholm when enabled in
[schedules.toml](../../../config/schedules.toml). Only publication shares the other adapters' queue. There is no weekly full run.

## Importable interfaces

`discover_provider(provider, client=...)` returns native catalogues. `fetch_metadata`
accepts a record containing `kpi`, `scope`, `years`, `references` and optional `modified`,
using the injected `KoladaClient`. Workflow orchestration validates replacements.
The standalone `kolada` retrieval entry point works in a plain installation:

```python
from retrieval.data_adapters.kolada import retrieve

result = await retrieve(
    session,
    provider_code="kolada",
    dataset_code="municipality/N78110",
    language="sv",
    selection=selection,
    data_url=document["extension"]["nordicintel"]["data_url"],
    metadata_url=document["extension"]["nordicintel"]["metadata_url"],
    metadata={
        "/dimension": document["dimension"],
        "/extension/kolada": document["extension"]["kolada"],
    },
)
```

The caller owns session retries, timeouts and pacing. Sparse output preserves zero,
missing (`m`), privacy (`p`), returned-without-value/reason (`e`) and unknown flags.
Deleted rows are excluded. Empty results preserve requested axes. Invalid selections
raise `ValueError`, missing endpoints `LookupError`; upstream errors and cancellation propagate.

## Verification

```powershell
uv run --extra harvest pytest tests/test_kolada.py tests/test_kolada_workflow.py tests/test_kolada_retrieval.py
uv run --extra harvest python scripts/live/check_kolada_live.py
```

The opt-in live script checks at most twelve documents across six fixed KPIs and
small retrieval selections, saving no observations. Fixture tests establish lifecycle
and error isolation separately. Actual verification status is in the consolidation inventory.

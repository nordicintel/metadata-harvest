# Implementing an adapter

Start with `registry.py` and the importable PXWeb packages. Registry entries are an
explicit small table, not a plugin system. Placeholder adapters raise
`NotImplementedError`, are excluded from jobs and have no retrieval entry points.

1. Define adapter-specific organisation, catalog and service configuration and complete language-specific
   discovery; return observed records, scope completeness and explicit failures.
2. Parse native metadata inside the adapter, then return `list[HarvestContribution]`
   from the public `fetch_metadata` boundary. Validate with the local Pydantic models
   before accepting any replacement; preserve unfamiliar fields in source extensions.
3. Keep complete source change signals in the accepted `source_record`; use shared
   storage and artifact helpers with adapter-owned discovery/reconciliation rules.
4. Supply typed distribution retrieval bindings and implement standalone async
   retrieval, importing only base dependencies. `REQUIRED_METADATA` declares the operational fields needed by the adapter;
   the optional catalog dispatcher resolves them internally.
5. Add fixtures, lifecycle/isolation tests, bounded live checks, complete automated
   acceptance and a subsequent reuse/byte-stability check before claiming readiness.
6. Deliver provider coverage, source mappings, preparation gaps and verification
   evidence in the same change as code; keep field-reference and package checks green.

Metadata jobs never persist observation cubes. Codelist failure is a dependency
failure, not a table-metadata failure. Future adapters must not clear existing output
before successful replacements. See [retrieval usage](../retrieval/README.md),
[contracts](../model/output-contract.md) and [prepared migrations](../migrations/consolidation.md).

## Required error isolation

The failure unit is an individual table, resource or discovery scope, never the
entire API output. Catch source, parsing and validation failures at that boundary,
record the full identity and error, and continue processing independent work.
Malformed catalogue rows must not prevent valid later rows or pages from being
processed. Untrustworthy pagination or language envelopes may stop that discovery
scope; other languages and providers still continue.

Publish validated successes together with current errors. Keep the last accepted
metadata for a failed table; a new failed table has no accepted document. Retry
failed tables on subsequent incremental runs even when their listing timestamp is
unchanged. Codelist failures are independent dependency errors and must not reject
valid table metadata. Never replace a failure with an empty dataset or erase a
provider directory before harvesting.

Only complete discovery proves absence. An incomplete scope preserves its existing
records, while valid discovered siblings can still be updated. Conflicting duplicate
identities are quarantined rather than choosing an arbitrary replacement. Scope
identities and request caches by provider, language and complete dataset path: two
similar URLs or identical basenames in different folders are independent resources.

Use batched atomic publication and report `completed_with_errors` when errors remain.
That status means accepted successes were published with explicit failures; it does
not mean all API output was discarded. Artifact integrity remains strict: stale runs,
unsafe paths, identity mismatches or invalid hashes block the affected untrusted
artifact. Do not confuse these publication safeguards with ordinary source errors.
Cancellation must propagate rather than being swallowed as a table failure.

Future adapter acceptance must demonstrate a bad table before valid siblings, a bad
listing row before later pages, similar URLs with different full paths, language and
provider isolation, preservation on failure, retry after failure, independent
codelist retries, and publication of successes alongside errors. Fixtures establish
these failure semantics; live checks establish actual provider compatibility.

## Required operational visibility

Future adapters must expose live counters and individual structured errors through
`Progress`, supervised by `apps.operations.monitor` in Actions. Keep telemetry
outside certified output. Report phase/current resource before slow work, update
counts after each outcome, and include full identities and URLs in errors.
Unknown totals, not-yet-attempted dependencies and retries are not failed tables.
Never claim progress from workflow status or an estimated request count.

Use independent adapter queues and provider-specific rate limits. Serialize only
publication, retain incremental reuse and resumable checkpoints, and never repeat
a complete harvest for an unrelated change. Acceptance must prove live read-back
of successes and individual failures before the worker exits, loss-of-monitoring
behavior, cancellation, and unchanged output on an incremental run. See the
[operational commands and heartbeat semantics](README.md#inspect-a-running-job).

## Scheduling and downstream catalog

Register a completed adapter in the explicit registry and add its automatic
schedule/enablement to [schedules.toml](../../config/schedules.toml). Generate workflow blocks
with the scheduling sync command; keep deliberate manual runs and checkpoint
continuations independent of the schedule. Do not introduce another enablement
variable or scheduler.

Publish accepted grouped collections and current reports before dispatching the
shared catalog workflow, even when individual resources failed. Catalog code must
never call an adapter's discovery/refresh/retrieval operations. Future enrichment
belongs in the shared stages described in [catalog processing](../catalog/README.md), not in
provider harvesting or a prerequisite that forces unchanged datasets to be fetched.

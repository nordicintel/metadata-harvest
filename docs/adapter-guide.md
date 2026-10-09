# Implementing an adapter

Start with `registry.py` and the importable PXWeb packages. Registry entries are an
explicit small table, not a plugin system. Placeholder adapters raise
`NotImplementedError`, are excluded from jobs and have no retrieval entry points.

1. Define adapter-specific provider configuration and complete language-specific
   discovery; return observed records, scope completeness and explicit failures.
2. Validate native source metadata, map without losing source meaning, and call
   `validate_metadata(..., adapter=...)` before accepting any replacement.
3. Keep complete source change signals in the accepted `source_record`; use shared
   storage and artifact helpers with adapter-owned discovery/reconciliation rules.
4. Implement standalone async retrieval and static `REQUIRED_METADATA`, importing
   only base dependencies; register only actually supported provider codes.
5. Add fixtures, lifecycle/isolation tests, bounded live checks, complete automated
   acceptance and a subsequent reuse/byte-stability check before claiming readiness.
6. Deliver provider coverage, source mappings, preparation gaps and verification
   evidence in the same change as code; keep field-reference and package checks green.

Metadata jobs never persist observation cubes. Codelist failure is a dependency
failure, not a table-metadata failure. Future adapters must not clear existing output
before successful replacements. See [retrieval usage](retrieval-guide.md),
[contracts](output-contract.md) and [prepared migrations](consolidation.md).

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

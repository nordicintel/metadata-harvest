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

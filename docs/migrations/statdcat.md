# Historical profile 1 migration: StatDCAT migration execution

This is the historical profile 1 execution record. The shared-service interpretation below is superseded by [profile 2 configuration](../configuration/README.md).


Approved scope: replace shared metadata with extensible Pydantic StatDCAT-AP 3.0.0 models; standard properties first, no redundant custom copies; preserve unknown attributes at every level; multilingual datasets; Kolada scope distributions; portal import/refresh/matching; SCB publication gate; coordinate-safe retrieval; two deterministic archives; offline verified cutover.

## Tasks

- [x] Models, generated schema and complete attribute mapping
- [x] Conversion, identities, multilingual and scope assembly, codelists
- [x] Portal snapshot, matching, refresh and scheduling
- [x] Adapter collection and retrieval migration
- [x] Incremental catalog assembly, retention and publication
- [x] Corpus conversion, fidelity audit, full tests and live retrieval

## Execution ledger

Branch: codex/statdcat-models. Original main remains untouched.
Pre-flight: all tasks share the typed Dataset/Distribution/CodeList contracts. Retrieval must retain exact native category order. Converted collections must preserve task-level contributions during partial updates. Existing observations remain outside the metadata migration.

User refinement: exactly one DataService per adapter/software name, globally shared across all providers and endpoints. Owner identity and catalogs remain separate. Reviewed all 85 configured deployments; explicit mappings cover cross-catalog owners. Do not create endpoint-, version-within-adapter-, or provider-specific services.

Identity audit: all 49 EntryScape configurations have an explicit owner disposition. Shared hosting remains unresolved rather than acquiring an invented owner. Known host-derived aliases are replaced by canonical organisation codes in accepted filenames and public identities; original task/routing identities remain in source evidence.

Verification completed before cutover:

- All 23,900 source metadata records round-trip exactly through the typed conversion, including previously unmapped attributes.
- Five live old/new retrieval comparisons returned identical requests and observation results: SCB v2, CSN PxWeb v1, Kolada municipality, Kolada organisational unit, and Ale EntryScape. Caller-owned sessions remained open.
- All 23,391 portal snapshot records passed an enrichment smoke check, including optional source fields.
- Wheel and source distribution built; isolated plain installation loaded all 85 legacy retrieval entry points without harvest-only dependencies.
- Bundled upstream SHACL diagnostics ran on four representative datasets. Missing descriptions, scheme titles/labels and service endpoints remain diagnostic; globally shared adapter services deliberately keep deployment endpoints in retrieval bindings.

Final review: a fresh read-only reviewer found three Important issues. Regression tests first reproduced all three, then passed after fixing singleton generic-endpoint matching, structural references after SCB filtering, and preservation of nested portal category/licence attributes. The final full suite passed all 979 tests; candidate integrity passed. The additional unchanged-input reuse run was skipped at the user's explicit request.

Corpus verification additionally exposed circular conflict evidence during codelist merging. Conflict snapshots now remain JSON-compatible and retain successive assertions, including distinctions between `False` and `0`. Inspection of 1,956 native codelist records found 64 language-version sequence differences; `ni:categoryOrder` now records these explicitly. Both defects have regression tests that failed before the fixes.

User matching refinement: keep every unmatched harvested dataset. Native comparison keys normalize URL encoding, lowercase, and strip whitespace/punctuation while stored identifiers remain unchanged. The matching investigation recovered provider-scoped native IDs from configured PxWeb API URLs, resolving CSN default-port/framework-case and Domstolsverket protocol differences. Normalization collisions remain ambiguous; titles and shared endpoints cannot establish identity.

Post-assembly preservation audit: all 23,900 source contributions reconstruct exactly from 18,502 accepted datasets across twelve adapters. The durable [audit ledger](statdcat-audit/README.md) pins original Git state, source and output hashes, per-attribute destinations and every old-to-new identity. Both archives passed the existing 100 MiB size gate. The additional unchanged-input reuse run was skipped at the user's explicit request. Accepted collections, models, schemas, documentation and catalog state have cut over in this branch.

Final [verification evidence](statdcat-audit/verification.json) distinguishes the 979-test automated suite, five live retrieval comparisons from saved accepted distributions, full source-preservation audit, package installation, and diagnostic upstream validation. Portal network verification covered two pages; the complete repository snapshot was validated offline. Existing provider harvest failures remain visible in catalog reports and do not indicate a migration assembly error.

# Organisation, catalog and service migration

Execution ledger for the approved three-file configuration and StatDCAT profile 2.0.0 migration.

- [x] Authoritative adapter, organisation and catalog configuration; packaged projections.
- [x] Concrete service and organisation models; standard catalog relationships.
- [x] Origin/context-aware EntryScape attribution and compatible worker loading.
- [x] Documentation, supplied reference, generated schema and inventory.
- [x] Targeted tests and offline accepted-collection candidate conversion.
- [ ] Production cutover/publication: explicitly deferred by user; production data, archives and state remain unchanged.
- [x] Commit implementation, configuration and documentation; production publication is excluded.

Ruling: preserve minted dataset/distribution IDs and routing aliases even when a catalog attribution is corrected. A host is a transport location, not an organisation.

Ruling: source attribution gaps remain explicit record findings. Accepted contributions are never removed to make configuration appear complete.

Ruling: configuration changes do not require provider reharvesting. Publication is for production only, not a development verification step. The publication pipeline was stopped before compression or replacement; no full-corpus unchanged-input reuse run was performed.

## Validation evidence

- 60 fixture checks passed for configuration, models, profile conversion, portal enrichment and catalog assembly, including failed-contribution retention.
- 273 fixture checks passed for runners, retrieval compatibility across adapters and EntryScape discovery/workflows.
- Three generated-document and packaging-contract checks passed; generated schemas and inventories are current.
- Wheel and source distribution built successfully; an isolated plain installation loaded all 85 compatibility retrieval entry points and packaged configuration.
- Ruff lint and formatting passed. The supplied HTML is deliberately byte-preserved, including its original whitespace.
- No live provider requests, reharvest or full-corpus unchanged-input reuse check ran for this migration.

The configuration contains 12 adapters, 78 organisations, 85 concrete service bindings and 86 catalogs. Six accepted EntryScape records lack sufficient catalog-attribution evidence; they remain accepted with explicit findings and unchanged dataset/distribution identities.

All 18,502 accepted datasets were converted into the separate local candidate at
`output/profile2-migration/`. Production files were not replaced. Accepted readers
support both profiles explicitly so the implementation does not depend on publishing
new archives first. The offline migration tool remains available for a production cutover.

The [accepted-collection migration ledger](profile2-audit/summary.json) records candidate hashes and per-record identity mappings. It is evidence of development conversion, not a production publication report.

# Trafikanalys migration preparation

Status: importable unavailable placeholder only; no job or retrieval entry point.

Source: [936b20eadd4ba9e748d6fb00c6ca67a7e414a290](https://github.com/nordicintel/trafa-adapter/tree/936b20eadd4ba9e748d6fb00c6ca67a7e414a290). Coverage: `trafa`; verify Swedish/English availability per product rather than assuming equal coverage.

## Reusable components

Reuse API structure parsing, dataset construction, source attribution and retrieval against `https://api.trafa.se/api`; existing construction combines structure with HTML documentation combinations and bounded result requests for units.

## Blockers and required changes

HTML discovery is an unresolved API-only boundary; decide whether equivalent complete enumeration is obtainable from API or explicitly authorize retaining HTML. Replace directory clearing. No dependable update signal is established.

Target the repository-owned Dataset and retrieval contracts, keep full native source
meaning, separate operational configuration from the minimal Provider schema and
retain current accepted content on transient failures. Do not import backend
services, destructive output clearing or observation databases.

## Future acceptance

Prove every published dataset combination and complete categories, source-title/unit/marker mappings and coordinate retrieval; demonstrate API-only discovery or an explicitly scoped exception before enabling a job.

Then require schema/category validation, language/adapter isolation, deterministic
grouped gzip, resumable independent automation, complete source accounting and a
subsequent incremental run before marking this adapter available.

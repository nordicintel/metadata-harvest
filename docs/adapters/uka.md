# UKÄ migration preparation

Status: importable unavailable placeholder only; no job or retrieval entry point.

Source: [ca533a715e7774a757056afb7cf55db80092f4dd](https://github.com/nordicintel/uka-adapter/tree/ca533a715e7774a757056afb7cf55db80092f4dd). Coverage: `uka`; Swedish source; English coverage remains unverified.

## Reusable components

This is an implemented external metadata/retrieval adapter, not merely a scaffold. Its recorded acceptance covers 113 indicators, 199 tests and full category discovery through 3,339 CSV exports (about 3.25 GB). Reuse indicator identities, category mappings and retrieval knowledge after scope approval.

## Blockers and required changes

Complete metadata currently depends on CSV exports, so importing it would cross this implementation's API-only/file-ingestion boundary; `/api/totals/{id}` alone is not demonstrated as a complete substitute. A separate scope decision is required.

Target the repository-owned Dataset and retrieval contracts, keep full native source
meaning, separate operational configuration from the minimal Provider schema and
retain current accepted content on transient failures. Do not import backend
services, destructive output clearing or observation databases.

## Future acceptance

After deciding the CSV boundary, prove full indicator/category coverage and source mappings, eliminate destructive replacement, provide independent grouped publication and live retrieval tests; do not claim support here from external acceptance.

Then require schema/category validation, language/adapter isolation, deterministic
grouped gzip, resumable independent automation, complete source accounting and a
subsequent incremental run before marking this adapter available.

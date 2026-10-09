# SDB / Socialstyrelsen migration preparation

Status: importable unavailable placeholder only; no job or retrieval entry point.

Source: [344212d51a28b848288e2beb90b7a5432bfa225e](https://github.com/nordicintel/sdb-adapter/tree/344212d51a28b848288e2beb90b7a5432bfa225e). Coverage: `socialstyrelsen`; Swedish and English, independently discovered (external baseline 15 Swedish and 5 English subjects).

## Reusable components

Reuse metadata discovery and `retrieval.py`, including `/extension/socialstyrelsen/result_dimensions` mappings for API field names that differ from dimension codes; retain classification child hierarchies, labels, units and source endpoints.

## Blockers and required changes

Replace destructive directory clearing with grouped reconciliation; no reliable source modification signal means full metadata refresh until proven otherwise; prove independent language completeness.

Target the repository-owned Dataset and retrieval contracts, keep full native source
meaning, separate operational configuration from the minimal Provider schema and
retain current accepted content on transient failures. Do not import backend
services, destructive output clearing or observation databases.

## Future acceptance

Validate hierarchy/result-field mappings against live samples in each language; complete independent subject discovery, metadata/category coverage, retrieval coordinate checks, retention on failures and confirmed deletion without cross-language loss.

Then require schema/category validation, language/adapter isolation, deterministic
grouped gzip, resumable independent automation, complete source accounting and a
subsequent incremental run before marking this adapter available.

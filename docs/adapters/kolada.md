# Kolada migration preparation

Status: importable unavailable placeholder only; no job or retrieval entry point.

Source: [a80bb03](https://github.com/rubenselander/kolada-data-scraper/tree/a80bb03). Coverage: `kolada`; Swedish source metadata.

## Reusable components

Reuse KPI, municipality and organizational-unit identities, KPI groups, municipality groups, organizational-unit groups and source change signals from the external v3 scraper; keep municipality and OU namespaces distinct.

## Blockers and required changes

Current exports depend on a stored observation database to infer availability and gender/category coverage. Establish complete metadata using source metadata endpoints and bounded live checks without introducing an observation warehouse; distinguish source changes from scrape timestamps.

Target the repository-owned Dataset and retrieval contracts, keep full native source
meaning, separate operational configuration from the minimal Provider schema and
retain current accepted content on transient failures. Do not import backend
services, destructive output clearing or observation databases.

## Future acceptance

Discover all KPIs/groups/entities, document source-change comparison and complete category derivation, verify municipality and OU retrieval, preserve flags/zero and show metadata-only full/incremental jobs with no persisted observation database.

Then require schema/category validation, language/adapter isolation, deterministic
grouped gzip, resumable independent automation, complete source accounting and a
subsequent incremental run before marking this adapter available.

# RowStore / EntryScape tables migration preparation

Status: importable unavailable placeholder only; no job or retrieval entry point.

Source: [27d6991ba649171c64d548a6cf7ef1f40d853bbf](https://github.com/nordicintel/rowstore-adapter/tree/27d6991ba649171c64d548a6cf7ef1f40d853bbf). Coverage: Provider identity and dataset languages must be established from source catalogue semantics; no code is registered here.

## Reusable components

The adapter repository is a scaffold; reusable HTTP transport exists in `rubenselander/swedish-data-api`. Catalogue entries are not automatically statistical cubes.

## Blockers and required changes

Define dataset, dimension, metric and category semantics before mapping. The existing consumer 20,000-row cap cannot prove complete pagination or category discovery. Establish reliable total/end-of-page evidence, duplicate handling and change signals.

Target the repository-owned Dataset and retrieval contracts, keep full native source
meaning, separate operational configuration from the minimal Provider schema and
retain current accepted content on transient failures. Do not import backend
services, destructive output clearing or observation databases.

## Future acceptance

Fixtures and live checks must exceed 20,000 rows, cover all pages/categories and detect changing pagination; validate cube semantics or explicitly use a separate catalogue contract, plus selection retrieval, deletion protection and source provenance.

Then require schema/category validation, language/adapter isolation, deterministic
grouped gzip, resumable independent automation, complete source accounting and a
subsequent incremental run before marking this adapter available.

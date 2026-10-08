# PXWeb v2 migration assessment

Reference code inspected on 8 October 2026. The SCB sv/en adapter is now
implemented here; see [execution, output and verification](pxweb-v2.md).
This reference assessment does not establish complete live acceptance.

| Implementation | Useful capabilities | Constraints / risks |
|---|---|---|
| [`rubenselander/PxWebAPI-scraper`](https://github.com/rubenselander/PxWebAPI-scraper/tree/fe8c19a29a5cf58c8ed60f7d1f58d98866e34cfb) | Provider config, multilingual paginated catalogue, update comparison, metadata, referenced codelists, pacing and incremental persistence | `_validate_metadata` only checks for a dict; codelist failures can retain stale lists or omit newly required ones; no Actions workflows |
| `nordicintel-adapter-pxweb2` local commit `2458eb25fe5ba208566d4906101bacb217e4638b` | Importable `PxWebAdapter`; per-language discovery including discontinued tables; `defaultSelection=false`; metadata and catalogue mapping; source marker includes period range | Coupled to core contracts and HTTP injection; no complete codelist publication path in `fetch_metadata`; pagination silently stops if page metadata absent |
| `nordicintel-harvest` / `nordicintel-backend` local v2 adapters | Existing catalogue mapping, notes/contact/subject parsing, source URL mapping and tests | Broader runtime dependencies; discovery breaks on failed page retrieval, so copying it without explicit completeness would risk deletion after partial discovery |

## Selection

Use the personal scraper's API-focused discovery, configuration, codelist and
incremental logic as the primary reference. Reuse the NordicIntel adapter's
explicit metadata selection and catalogue mapping where they improve completeness.
Keep complete metadata payloads instead of rebuilding dimensions from a reduced
catalogue record. Do not transplant its SQLite persistence or observation methods
into the first adapter: use this repository's grouped latest-document publication.

The personal implementation is **not an all-or-nothing language refresh**:
`_stream_table_metadata` submits successful tables to the database insert worker
and waits for those writes before raising failed-request results. Codelists are
upserted incrementally; final `language_refresh` replaces the desired codelists
and prunes unseen tables. Its summary distinguishes table/language/codelist counts.
Successful persistence cannot by itself establish complete publication.

## Implementation scope

The SCB implementation adds `adapters/pxweb_v2` with importable discovery and metadata
retrieval, a separate provider configuration and fixtures covering `/config`,
every `/tables` page, `/tables/{id}/metadata` and referenced `/codelists/{id}`.
Preserve multilingual categories and complete codelist payloads. Use the grouped
JSONL pattern established by v1 under its own `data/pxweb_v2/` directory: merge validated successes, retain failed prior
documents, and expose incomplete runs in reports.

Before enabling its own manual/scheduled workflow, verify:

- Duplicate IDs, missing pages or changing pagination totals mark discovery
  incomplete and cannot confirm deletion of missing datasets.
- Failed metadata retains the previous accepted table. Failed codelists are
  separate dependency errors and do not block valid new or updated table metadata;
  unchanged tables are reused while failed dependencies are retried.
- `defaultSelection=false`, full categories and discontinued tables are covered.
- Source update markers and retrieval timestamps have distinct meanings.
- Repeated unchanged runs preserve JSONL bytes; changing only one language cannot
  rewrite or remove another language's accepted documents.
- Worker output, collector validation and persisted checkpoints survive partial
  failures and continuation; bounded live checks precede a full automated run.

The new v2 schedule is gated by `PXWEB_V2_ENABLED=true` until acceptance.
No superseded schedule has been disabled.

# SCB PXWeb v2

Implementation: `apps/harvest/metadata_adapters/pxweb_v2/`.

## Collection

Read `/config`, all language-specific `/tables` pages, table `/metadata` and referenced
`/codelists`. Include discontinued tables while accessible. Harvesting does not call
observation or saved-query endpoints. Discovery completeness is independent by language;
different Swedish/English counts are valid.

`fetch_metadata` returns typed contributions for shared multilingual assembly.
`fetch_codelist` returns a typed SKOS scheme. Native codelist caches remain grouped
by source/language, separately from accepted dataset collections and final catalog
streams. Deduplicate native codelist requests per language.

## Updates and recovery

Incremental runs compare current listing records with successfully accepted state,
fetching new, changed, invalid and previously failed records. Full runs also catch
changes without updated listing timestamps. Valid unchanged codelists can be reused
during dependency-only retries. Source changes during collection receive a bounded
catalog/metadata retry.

A codelist failure is reported and retried independently; valid table metadata still
advances. Complete discovery or confirmed repeated inaccessible responses establish
removal, while incomplete discovery preserves accepted records. Keep dangling native
references accompanied by explicit dependency errors.

Workers retain request limiting, deterministic batches, run mode and completed tasks
across continuations. Collectors reject stale baselines and invalid artifacts.
Recovered cancelled-run artifacts must validate before reuse. Full/incremental
schedules come from `config/schedules.toml`, not a hard-coded weekly rule.

## Shared interfaces

[Harvesting operations](README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../model/output-contract.md) defines accepted metadata;
[retrieval usage](../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../migrations/README.md).

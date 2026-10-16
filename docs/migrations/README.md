# Migrations and historical evidence

Current metadata uses [profile 2.0.0](../model/output-contract.md).
`apps.migrations.profile_reader.read_dataset` reads historical accepted profile 1
records without persistence. Ordinary collection/catalog reads do not run a corpus
migration. Native worker projections remain active harvesting internals; their
schema filename includes `legacy` but they are not public metadata.

## Explicit offline conversion

Use separate candidates and inspect each command's `--help` before choosing inputs:

```sh
uv run python -m apps.migrations.collections --source data --output output/statdcat-migration/data
uv run python scripts/migrations/migrate_profile2.py --source data --output output/profile2-migration
uv run python scripts/migrations/verify_statdcat_migration.py --help
```

The first converter handles the original worker corpus; the second upgrades profile
1 identities/relationships into profile 2 while recording mappings. These are
different source formats, not sequential routine maintenance. Conversion is offline
and candidate-only; production promotion is separate. Git history retains prior code
and committed source bytes. Do not reharvest or recompress production to verify docs.

## Evidence inventory

These artifacts describe their recorded runs, primarily October 2026. Counts, test
results, source availability and hosted-check limitations are historical, not current
readiness claims.

| Evidence | Meaning |
| --- | --- |
| [StatDCAT audit](statdcat-audit/README.md) | Per-record and per-attribute preservation ledgers, source/output hashes and reconstruction results for the original migration |
| [Profile 2 summary](profile2-audit/summary.json) and adjacent compressed ledgers | Development conversion, preserved identities/contributions and candidate hashes; not evidence of production promotion |
| [Verification receipts](verification/) | Earlier conversion, fixture and bounded/live adapter checks; scope is recorded per receipt |
| [Compression trial](../publication/compression-trial.json) | Historical corpus size/timing and content-hash comparison; not a future size guarantee |
| [Supplied specification](<../reference/StatDCAT-AP 3.0.0.html>) and [provenance](<../reference/StatDCAT-AP 3.0.0.provenance.json>) | Verbatim reference snapshot, independent of the pinned model revision |
| [EntryScape origins](../reference/entryscape-origins/) | Host, catalog/context and publisher discovery evidence; not runtime inventories |
| [PXWeb exclusion record](../harvest/excluded-providers.json) | Explicit owner decisions, checked against the configured providers by regression tests; Västerås was restored on 2026-10-16 once JSON-stat fallback existed |

Completed implementation plans and delivery narratives are retained in Git history.
Preservation ledgers and supplied references remain byte-identical; update maintained
guides rather than appending another historical status page.

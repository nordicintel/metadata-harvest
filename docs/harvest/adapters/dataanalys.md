# Socialstyrelsen analysverktyg (`dataanalys`)

Implementation: `apps/harvest/metadata_adapters/dataanalys/`;
routing alias `socialstyrelsen_analys`.

## Scope and source mapping

One dataset represents an indicator within an area: `{AREA}|{MBusinessKey}`.
Superset charts and dashboards are supporting resources, not dataset identities.
Find paired semantic Landningssida/Trender datasources using a case-insensitive
schema prefix. Probe source columns because area-specific spellings differ;
retain the resolved column map for retrieval.

Coverage is Swedish Riket/Region statistics. Municipality, care-unit, MART tables
and the unsupported NPS profession layout are not represented by this model.
An unsupported new area is distinct from a failure to refresh an already accepted
area. Region categories use source names, not invented SCB codes.

Prefer an indicator's catalog dashboard link, then published chart filters, then
the area's overview. Incomplete dashboard discovery must not justify an unreliable
fallback. Layout modification timestamps are not source data modification times.

## Refresh and limitations

No complete category-change signal exists; rebuild selected indicators and reuse
identical accepted content. `--subject` selects areas; `--indicator` accepts native
or area-qualified IDs. Bounded runs remove nothing. Quoted IDs containing `|` avoid
shell interpretation. Incomplete listing counts, duplicate IDs and malformed rows
retain accepted scope state. Currentness/withholding uses endpoint status and
configured `max_age_days`.

Retrieval queries resolved columns and splits large Region/Time selections.
Comments become status; null-without-comment cells stay absent. Conflicting duplicate
cells and failed queries are errors, not successful empty results. Superset can serve
cached data; source timestamps and verified-at timestamps remain separate.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../../migrations/README.md).

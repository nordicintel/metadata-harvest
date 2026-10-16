# Kolada

Implementation: `apps/harvest/metadata_adapters/kolada/`.

## Identity and scope

Group by native KPI ID under organisation `rka`; the source routing alias is `kolada`.
Municipality and organisational-unit scopes are separate distributions of that KPI,
each retaining categories and coverage. Municipality scope covers the static inventory
in `schemas/kolada_municipalities.json`: municipalities, regions, the national
aggregate and the useful comparison groups. Derivable comparison groups outside that
inventory are ignored. OU retains parent municipality relationships without pretending
to be the same geographic dimension.

Include gender where male/female rows exist, including suppressed rows. Total-only
datasets omit that dimension and use T values. The OU, municipality-group and
KPI-group listings are stored once in state; each dataset carries its relevant members.

Years are fixed by scope: municipality values span 1970 to two years ahead (forecast
periods); OU values span 2003 to the current year and are never in the future.

## Refresh

Each run lists the KPI catalogue and reference tables, then scans every cell added or
deleted since the last completed refresh with one day of overlap. The scan is batched:
25 municipalities (or 25 OU KPIs) by 25 years per request, all windows concurrently
with a shared rate limit. A week of changes takes minutes rather than hours.

Added cells fold into the saved coverage, and only the affected KPI scope is rebuilt
and validated. Any deleted cell forces a clean rescan of that single KPI across all
years, so coverage never retains withdrawn periods or entities. KPIs that leave the
catalogue are removed; a catalogue listing fewer than half the known KPIs is treated
as a discovery failure and removes nothing. Metadata-only changes (KPI record or
referenced members) rebuild from saved coverage without reading values.

The `updated` date is derived, never probed: a past publication date is used as is,
a future one minus one year; otherwise a portal modified date when state carries one
(`references.portal_modified`); otherwise the first day after the last covered year,
capped at today.

The refresh marker advances only after an unrestricted run whose both scans
succeeded. Failed rebuilds and rescans stay pending and are retried next run; the
previous document is kept. Use `--kpi N78110 U30750` and `--scope ou` to bound work;
such runs never advance the marker. There is no full-rescrape mode and no multi-wave
continuation: a run finishes or leaves pending work for the next scheduled run.
Consumer retrieval retains sparse zeroes and provider flags; deleted rows are excluded
and transport errors propagate.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.

## Stored source data

Each scope's envelope holds its KPI row; the coverage distilled from value cells, the computed `updated` date and the references digest are `derived`. The four listings are shared source files.

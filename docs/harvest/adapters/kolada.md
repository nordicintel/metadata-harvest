# Kolada

Implementation: `apps/harvest/metadata_adapters/kolada/`.

## Identity and scope

Group by native KPI ID under organisation `rka`; the source routing alias is `kolada`.
Municipality and organisational-unit scopes are separate distributions of that KPI,
each retaining categories, coverage, retrieval settings and failure state. Municipality
scope includes regions, national aggregates and configured groups. OU retains parent
municipality relationships without pretending to be the same geographic dimension.

Include gender where male/female rows exist, including suppressed rows. Total-only
datasets omit that dimension and use T values. Reference catalogs are stored once
in state; each dataset carries its relevant members and source evidence.

## Refresh

Refresh native catalogs and change signals, then rebuild only new, changed or failed
KPI scopes. Metadata-only changes reuse saved categories; data changes read the
scope's rows to infer coverage and immediately discard values. Inclusive date overlap
protects same-day updates. Accepted markers advance only after successful replacement.

Incomplete discovery cannot remove a KPI. Verify missing identities directly;
complete empty coverage can remove an individual scope. A failed scope never erases
its successful sibling. Discontinued accessible data remains eligible.

Use `--kpi N78110 U30750` and `--scope ou` to bound work. Restrictions survive an
explicit continuation, but normal new runs start fresh change checks. There is no
full-rescrape mode. Consumer retrieval retains sparse zeroes and provider flags;
deleted rows are excluded and transport errors propagate.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../../migrations/README.md).

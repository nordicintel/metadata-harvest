# SMHI emissions

Implementation: `apps/harvest/metadata_adapters/smhi_emissions/`; routing alias `smhi`.

## Metadata boundaries

One dataset per substance prevents mixing tonnes and CO2 equivalents in one measure.
`getfilteroptions/` supplies substances and the sector tree. Keep the full sector
tree: its listed substance memberships are evidence, not a complete availability
filter. Region codes come from the maintained static `regions.py` mapping because
the API provides no region catalog.

Only the current submission is harvested; omit the submission parameter rather than
sending the literal `undefined`. Observation probes infer metadata without persisting
values. Excel exports are not part of the implementation.

## Refresh and source limitations

Rebuild against current options and retain accepted state on mapping failures.
Endpoint availability and `max_age_days` control withholding. An unknown sector
label is an error requiring mapping review, not a reason to ignore source rows.
Geographic changes require updating the static registry.

Consumer retrieval translates preserved region codes into county/municipality
request parameters, groups sector requests, and enforces its request cap. Labels
map responses back to retained sector codes. Provider zeroes remain zeroes even
where the API cannot distinguish missing combinations; absent years stay absent.
Sector-specific Total rows must not duplicate their parent total. Units for some
substances and equivalence of geographic totals need source verification; do not
infer them from numerical plausibility.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../../migrations/README.md).

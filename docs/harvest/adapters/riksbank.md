# Riksbank

Implementation: `apps/harvest/metadata_adapters/riksbank/`; configured language `en`.

## Dataset and time boundaries

SWEA groups with their own series form datasets (`swea_group_<id>`); forecast series
form `mpd_<series_id>` datasets with a PolicyRound dimension; SWESTR fixing is a
separate dataset. Holdings, turnover, SWEA cross rates/aggregates and SWESTR compounded
averages/index are outside the implemented harvest scope.

Daily time dimensions span source boundaries and may include days with no banking
observation; retrieval leaves these cells absent. Forecast periods follow spacing
in the latest vintage, not an assumed series-ID frequency letter. Preserve policy
rounds even when a particular series has no vintage in one round.

SWEA units come from the explicit mapping where supported, never guessed for new
groups. A group is discontinued only when all its series are closed. Retain source
notices as notes and native per-series date/closure evidence.

## Requests and refresh

Rebuild from fresh catalogs and bounded latest-observation probes; never request the
unfiltered forecast corpus to infer metadata. `--subject` accepts dataset, group or
series selectors. Each API's discovery status remains independently visible.
Currentness uses working endpoints and configured `max_age_days`.

Respect the configured slow pacing and Retry-After. Harvesting optionally reads
`RIKSBANK_SUBSCRIPTION_KEY` and sends it as `Ocp-Apim-Subscription-Key`; never log it.
Retrieval uses the caller's session and does not read repository secrets. If needed,
the consumer configures authentication on its own session.

Unknown group units and vintage coverage remain explicit limitations. Source rates
are indicative; do not infer transactional suitability from successful retrieval.

## Shared interfaces

[Harvesting operations](../README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../../model/output-contract.md) defines accepted metadata;
[retrieval usage](../../retrieval/README.md) defines consumer calls and results.

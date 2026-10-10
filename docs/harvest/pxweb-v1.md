# PXWeb v1

Implementation: `apps/harvest/metadata_adapters/pxweb_v1/`.

## Discovery and metadata

`get_pxweb_tables` uses rendered website listings. API-form inputs are converted to
web routes; browser API traffic is blocked. Only explicit flat-list and cached
parent-folder enrichment requests fill missing listing metadata. They never add
API-only tables. Preserve actual links, full native paths, category numbering and
source date precision; similar basenames are not the same table.

`languages=None` tries English and Swedish; a URL language overrides it. An explicit
empty database list selects none. Automatic discovery may skip unusable databases
with diagnostics; explicit database requests remain strict. Timeouts and incomplete
traversal remain failures. Chromium contexts isolate website sessions.

`get_pxweb_metadata` uses GET metadata and sequential JSON-stat2 POST slices to cover
every category within provider limits. It discards observations, retaining units,
notes, flags and elimination evidence. A category-covering probe is not a download
of every observation combination. Accepted POST titles take precedence over GET
and listing fallbacks. Advance processed source timestamps only after acceptance.

## Source-specific constraints

- SCB HVD's `uppdrag/HV01__HV0101/...` route contains a real `HV01` API component;
  retain it without inventing a displayed category. HVD is separate from SCB v2.
- Göteborg enrichment needs provider pacing, including parent lookups and retries.
- Socialstyrelsen's separate PXWeb listings do not establish SDB coverage.
- [Excluded sources](excluded-providers.json) record explicit decisions; do not
  restore them merely because website discovery works.

Use `scripts/live/check_pxweb_live.py` for explicit discovery checks and
`scripts/live/check_metadata_live.py` for metadata checks. Bounded samples do not
establish complete source acceptance; exact options are in each script's `--help`.

## Shared interfaces

[Harvesting operations](README.md) defines collection, recovery and schedules.
[StatDCAT mapping](../model/output-contract.md) defines accepted metadata;
[retrieval usage](../retrieval/README.md) defines consumer calls and results.
Historical live/fixture receipts are indexed under [migration evidence](../migrations/README.md).

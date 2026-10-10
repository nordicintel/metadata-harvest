# Repository structure cutover

The agreed root plan is implemented by moving repository applications into `apps/`
and keeping `retrieval/` as the sole installed library. There are no forwarding
modules for the retired harvesting namespace or retrieval adapter path.

## Decisions preserved

- Pydantic metadata profile remains 2.0.0 with the existing upstream revision and vocabulary IRIs.
- Authoritative inventories and settings live in `config/`; duplicate packaged inventories were removed.
- Current worker conversion/projection stays in harvesting. Pure composition stays in model so accepted persistence does not depend on catalog orchestration.
- Historical profile reading is separate from migration commands. Existing explicit import/conversion script entry points delegate to application functions.
- Candidate construction can write temporary archives; production promotion remains explicit.
- Old candidates with missing or stale dependency fingerprints are rejected. Existing production state and archives are not rewritten.
- The obsolete concatenation builder and inactive enrichment placeholders were removed. Their implementation-specific tests were retired; streaming archive tests and current-candidate integrity tests cover the supported path.

## Verification

The implementation was checked with focused configuration/model/catalog/command
fixtures, import-direction guards and an isolated retrieval-only wheel installation.
All 85 entry-point names are retained. A structural comparison of the moved adapter
implementations found no request/response logic changes beyond imports. Live provider
checks are separate developer commands, not evidence from this structural cutover.

Production data, portal snapshots and archives are excluded from the change. No
provider harvest, corpus conversion, production compression or full-corpus reuse
check is required to apply it.

### Acceptance results (2026-10-10)

- Full offline suite: 1,042 passed, three failures caused by relocated fixture paths.
  All three paths were corrected and the three failed tests passed on rerun; the
  affected Tillväxtverket mapping/retrieval group also passed all 44 tests.
- New architecture and candidate gates passed (10 tests), and the additional
  historical 7-Zip fixture reader passed. No production archive was processed.
- Generated schemas/docs, documentation links, schedule synchronisation, Ruff
  checks and formatting passed.
- Isolated wheel installation loaded all 85 entry points and executed fixture
  dispatch with no repository application modules or dependencies installed.
- Independent read-only review found no important regression.
- Tracked production data is unchanged; relocated configuration and the saved
  StatDCAT reference/provenance are byte-identical to their originals.

These are offline fixtures and local checks, not new live-provider verification.

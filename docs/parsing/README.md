# Parsing source files into accepted collections

Harvests store what sources return; parsing turns those files into the accepted
StatDCAT collections that the catalog reads. Parsing is manual and unscheduled: it
runs for large-scale testing or before a production catalog build, never from a
harvest workflow.

## Source layout

```text
data/<adapter>/source/manifest.json
data/<adapter>/source/<provider>/<quoted dataset code>.<lang>.json          envelope
data/<adapter>/source/<provider>/<quoted dataset code>.<lang>.<part>.<ext>  text companion
data/<adapter>/source/<provider>/_shared/<name>                             shared responses
```

The manifest lists every entry (`<provider>/<dataset code>/<lang>`) with its files,
the source's own update signal stored verbatim as `updated`, and `retrieved_at`.
Refresh logic compares against the manifest; a missing entry means fetch.

An envelope holds `responses` (bodies as received) and `derived`. `derived` exists
only where the per-dataset source data are observations and cannot be stored:
Kolada coverage, EntryScape trimmed table structure, PXWeb v1 samples stripped of
`value`/`status`, and UKÄ per-period scan summaries. Everything else is raw.

Harvests store a body only when the client returned it successfully, it is not
empty, and it serialises as JSON without NaN. There is no mapping check at harvest
time: a source regression replaces the stored file and parsing reports the failure.

## Commands

```sh
uv run python -m apps.operations.parsing build --adapter trafa          # output/parsing/trafa
uv run python -m apps.operations.parsing apply --adapter trafa          # replace data/trafa/metadata
uv run python -m apps.operations.parsing publish --adapter trafa        # scoped Git commit/push
uv run python -m apps.operations.parsing build --all
```

`build` validates every document, writes `metadata/*.jsonl.gz` through the same
assembly as before, and reports per-key failures in `reports/parse.json` without
dropping siblings. `apply` refuses when the committed collections changed since the
build, when the adapter has no source entries yet, and when fewer datasets parsed
than the committed collections hold (pass `--allow-shrink` to accept a real
reduction). During the cutover each adapter's `source/` fills up over its first
full harvests; compare `source_entries` and `committed_datasets` in the build
report before applying. Tillväxtverket's data.europa.eu descriptions come from the offline portal
snapshot (`--portal`, default `data/data_eu`).

## Adapter contract

Each `apps/harvest/metadata_adapters/<adapter>/parse.py` provides
`documents(store, state, providers, *, portal=None)` yielding `(key, document)` or
`(key, exception)` per manifest entry, and `withheld_keys(state)` for entries whose
endpoint was unavailable or stale and must not be published.

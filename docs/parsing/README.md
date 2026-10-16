# Parsing source files into accepted collections

Harvests store what providers return; parsing turns those files into worker
documents, the accepted collections under `data/<adapter>/metadata/`. Parsing is
manual and unscheduled: it never runs from a harvest workflow.

## Source layout

```text
data/<adapter>/source/manifest.json
data/<adapter>/source/<provider>/<quoted dataset code>.<lang>.json          envelope
data/<adapter>/source/<provider>/<quoted dataset code>.<lang>.<part>.<ext>  text companion
data/<adapter>/source/<provider>/_shared/<name>                             shared responses
```

The manifest lists every entry (`<provider>/<dataset code>/<lang>`) with its files,
the source's own update signal stored verbatim as `updated`, and `retrieved_at`.
An envelope holds `responses` (bodies as received) and `derived`, which exists only
where the per-dataset source data are observations that cannot be stored.

## Commands

```sh
uv run python -m apps.operations.parsing build --adapter trafa          # output/parsing/trafa
uv run python -m apps.operations.parsing apply --adapter trafa          # replace data/trafa/metadata
uv run python -m apps.operations.parsing publish --adapter trafa        # scoped Git commit/push
uv run python -m apps.operations.parsing build --all
```

`build` asks the adapter's `parse.documents` for every entry, stamps the adapter
name into `extension.nordicintel`, validates each document, writes one
deterministic `metadata/<provider>__<digest>.jsonl.gz` per provider and reports
per-key failures in `reports/parse.json` without dropping siblings. Withheld
entries are skipped. `apply` refuses when the committed collections changed since
the build, when the adapter has no source entries yet, and when fewer datasets
parsed than the committed collections hold (pass `--allow-shrink` to accept a real
reduction). Tillväxtverket's data.europa.eu descriptions come from the offline
portal snapshot (`--portal`, default `data/data_eu`).

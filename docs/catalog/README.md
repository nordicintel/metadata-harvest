# Catalog construction and publication

The pipeline is offline: accepted contributions → grouping → portal matching →
enrichment → SCB filtering → validation → deterministic JSON-LD. Accepted
contributions come from `apps.operations.parsing apply` (see
[parsing](../parsing/README.md)); building never starts a harvest or a parse,
downloads a snapshot or promotes production output.

## Portal input

The accepted snapshot is `data/data_eu/oppnadata.jsonl.gz`, with a hash/count manifest.
`apps.portal.snapshot` accepts complete `oppnadata` snapshots only. Failed pagination,
invalid scope, malformed records or duplicate identities retain the previous snapshot.

```sh
uv run python -m apps.operations.portal import --source export.jsonl --output output/portal
uv run python -m apps.operations.portal refresh --output output/portal
```

These examples use a scratch destination. The default destination is `data/data_eu`;
Git publication is a separate `apps.operations.portal publish` command. Automatic
refresh is configured in [schedules.toml](../../config/schedules.toml).

## Matching and enrichment

Precedence is explicit [overrides](../../config/catalog-matches.json), source-scoped
native identifiers, then exact dataset-specific access, metadata or landing URLs.
Comparison keys normalize URL encoding, lowercase, whitespace and punctuation;
original identifiers remain intact. Titles and shared endpoints cannot prove identity.
Collisions remain ambiguous. Broader related portal entries are not equivalent datasets.

Harvested titles, structure, retrieval and coverage take precedence. Portal editorial
metadata and additional distributions enrich matches, with conflicts retained as
source assertions. Distribution licences remain scoped. Unmatched harvested datasets
stay included; portal-only datasets are not added.

`catalog.include_scb_pxweb_v1` in [settings.toml](../../config/settings.toml) filters only SCB's
main v1 distributions. HVD and other providers remain. Enabling it restores retained
descriptions at the next build without a source harvest or portal download.

## Candidates and production

```sh
uv run python -m apps.operations.catalog build --data data --output output/catalog --config config
# Explicit production promotion and scoped Git commit/push:
uv run python -m apps.operations.catalog publish --data data --output output/catalog --config config
```

The two output streams are `catalog.jsonl.zst` and `codelists.jsonl.zst`: Zstandard
level 6, checksums, deterministic JSONL and inline contexts. State and coverage reports
accompany them. Every published
file must stay below 100 MiB; development work does not require production publication.

Candidates record input, implementation, profile, matching-rule, configuration,
prior-catalog and output fingerprints. Source drift, tampering, stale dependencies
or unrelated staged changes block publication. Git publication is scoped, serialized
and never force-pushed.

## Reuse and recovery

Unchanged groups reuse verified output. Relevant portal inputs affect matching
fingerprints; unrelated portal records do not invalidate every group. Accepted Git
blob identifiers allow recovery of failed current collections. If source recovery
is impossible, previous verified owner output is retained and incompatible rule
changes are refused. Collection absence is not deletion evidence; a valid accepted
replacement must confirm removal. Reports separate matching, ambiguity, filtering,
failed inputs and retained contributions.

## Streaming reads

Repository tools can read either supported archive format:

```python
import io
import json
from apps.publication.archives import open_catalog

with open_catalog("catalog.jsonl.zst") as raw:
    with io.TextIOWrapper(raw, encoding="utf-8") as stream:
        for line in stream:
            dataset = json.loads(line)
```

This is sequential decompression, not indexed random access. Consumer streaming
requires no extracted file; the assembler may use temporary files for byte-range
reuse. `apps` and decompression tools are not part of the installed retrieval library.

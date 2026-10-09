# Metadata output contract

Publish one collection per provider/language inside each adapter's directory.
V1 uses plain `.jsonl`; v2 uses lossless `.jsonl.gz` for metadata and codelists.
Consumers must stream each nonempty JSONL line as an independent JSON object,
decompressing gzip collections first. There are no per-table result files.

## Statistical metadata

PXWeb v2 retains native SCB metadata with a source-specific catalogue record;
its codelist collections and identity fields are described in the
[v2 output contract](pxweb-v2.md#output-and-ownership). The v1 mapping below
remains unchanged.

Both adapters store identity `(provider_code, dataset_code, language)` inside
`extension.metadata_harvest`. Documents have `class: "dataset"`, `version: "2.0"`,
`label`, `updated`, `id`, `size`, `dimension`, `value: []` and `extension`.
The metadata-only JSON-stat2 representation retains actual category counts and
dimensions but contains no observation cube. Do not describe `value: []` as a
populated JSON-stat cube or infer coverage of every observation combination.

V1 mapping:

| Input | Output / rule |
|---|---|
| Listing `id`, configured provider, language | Identity in `extension.metadata_harvest` |
| Listing title | `label`; listing takes precedence over GET and sampled POST |
| Listing `published` | `updated`, preserving provider precision; not harvest time |
| All known locations | `extension.metadata_harvest.paths`; grouped across databases/categories |
| GET variable codes and complete values | `id`, `size`, dimension category index and labels |
| GET + bounded JSON-stat2 POST metadata | Units, notes, links, roles, extensions; observation values discarded |
| GET-only fields lacking standard equivalents | `extension.metadata_harvest.pxweb` at dataset/dimension level |
| Sample status | Coordinate-scoped `sample_status`; not a full-cube status array |
| Synthetic eliminated dimensions / roles | `auxiliary_dimensions` / `auxiliary_roles` |

Harvest times (`started_at`, `finished_at`, report `updated_at`) live in run reports
and checkpoints. Dataset `updated` is the last successfully built source marker;
a failed replacement must not advance it. Do not invent an upstream timestamp
when a future source only provides retrieval time.

`storage.validate_dataset` enforces v1 publication invariants: identity,
successful source timestamp, retained paths, dimensions/category counts and no
observations. Source mapping additionally validates category coverage. This is
not a claim of full external JSON Schema or StatDCAT-AP validation. V2 uses its
own native metadata/codelist validation; it does not require v1 listing paths.

## Files and migration

| Collection | Format |
|---|---|
| `data/pxweb_v1/metadata/<provider>__<language>__<digest>.jsonl` | V1 dataset metadata |
| `data/pxweb_v2/metadata/<provider>__<language>__<digest>.jsonl.gz` | Native v2 dataset metadata |
| `data/pxweb_v2/codelists/<provider>__<language>__<digest>.jsonl.gz` | `{provider_code, language, codelist}`; not datasets |

Each metadata line is the latest accepted source-available dataset. Each adapter owns
its separate `table-records/`, `reports/refresh.json`, `reports/harvest.json` and
unfinished `work/current.json`. Confirmed-absent datasets are deleted without an
archive. Reports are overwritten; the checkpoint is removed on completion.
A failed request or incomplete listing does not by itself prove source removal.
A digest
prevents case-insensitive filename collisions; quoted identifiers prevent path
traversal. Lines are sorted by stable dataset task ID, object keys are sorted,
UTF-8 is retained and each line ends in LF. Identical content is not rewritten.
V2 uses deterministic gzip headers, preserving byte-identical compressed output
when content is unchanged. A codelist failure does not invalidate its table:
missing references are accounted for by explicit codelist errors in the report.

The former `filename` field in continuation tasks remains a stable **logical
record key**, not a physical output path. Retaining it lets old checkpoints and
worker artifacts be consumed during migration. New workers emit grouped JSONL.

```python
from metadata_harvest.storage import JsonlStore, migrate_storage

# Explicit local migration, no provider traffic. Verifies every document.
counts = migrate_storage("data/pxweb_v1")
documents = JsonlStore("data/pxweb_v1/metadata").records.values()
```

Migration reads legacy JSON, stages and verifies JSONL before removing old files,
rejects conflicting duplicate identities and verifies round-trip equality.
Collectors also read the legacy layout and write only grouped output. Empty
collections are removed after replacement verification. No metadata versions or
source-removed records are kept in the current output tree.

Atomicity is per collection file. Only a completed collector's Git commit
publishes the full state. If a collector crashes during multi-file replacement,
inspect its artifact and restart from the last committed state; do not publish
an unverified partial working tree. Never run multiple writers against one data
directory. Production Actions already serialize publication.

## Schema ownership and consumers

V1 uses Playwright; v2 uses httpx. Neither depends on a schema/model package.
Confirmed consumers are this repository's runner, collectors, diagnostic scripts,
tests and documented Python public functions. An 8 October 2026 search of the local backend,
harvest, model and v2 adapter checkouts found no direct `metadata-harvest` import
or path reference; this is not proof that no external file consumer exists.

The actual related repositories are `nordicintel/nordicintel-schemas` and
`nordicintel/nordicintel-model` (the handoff's short names do not resolve as given).
The schema revision inspected on 8 October 2026,
`77a7a4665fea6f3c2be8f286d560b79317f08dd7`, describes metadata-only
JSON-stat2 and a prepared, unpublished 2.0.0 contract using
`extension.nordicintel`. Model checkout `73017b7938685f7cdbc71c05ca804b9901552452`
contains immutable JSON-stat and PXWeb codecs. Neither is copied or silently
adopted here: the current `extension.metadata_harvest` contract differs.

Before changing these fields, read the complete schema specifications and adopted
standards, including relevant StatDCAT-AP requirements; map actual source needs,
explain departures and plan consumer updates. StatDCAT-AP representation remains an
open decision; catalogue metadata must not be forced into a complete cube contract.

# Published output contract

One deterministic `.jsonl.gz` metadata collection per adapter, provider and language
lives under `data/<adapter>/metadata/`; v2 codelists live under `codelists/` in separate
collections. Dataset identity is `(provider_code, dataset_code, language)` in
`extension.nordicintel`. Read gzip as UTF-8 JSON Lines, one independent document per line.

## Shared Dataset mapping

The [packaged schema](../src/metadata_harvest/schemas/dataset-metadata.schema.json),
[semantic contract](DATASET-METADATA.md) and [field reference](DATASET-METADATA-REFERENCE.md)
are authoritative. Metadata requires complete dimensions/categories, `value: []`
and no root observation `status`. The Provider schema is a minimal consumer contract;
`providers*.json` remains separate operational configuration.

| Source | Canonical mapping and retained evidence |
|---|---|
| Configured provider, source dataset code, language | `extension.nordicintel`; never substitute an internal generated dataset code |
| Accepted API metadata title | Root `label`; listing-only refresh does not overwrite it |
| Source `updated` / v1 listing `published` | Root `updated` calendar date when a valid full date is available; original precision/timestamp in `extension.<adapter>.updated_original` |
| Full accepted catalogue/listing record | `extension.<adapter>.source_record`; incremental comparisons use source signals, not truncated root dates |
| API/web URLs and thematic paths | Shared `metadata_url`, `data_url`, `source_url`, `paths`; literal URL characters are encoded |
| v2 period, time unit, subject | Shared `first_period`, `last_period`, normalized `time_unit`, `subject`; original values remain in the catalogue |
| Contacts | Shared `contacts`, `mail` becomes `email`; unparsed contact prose remains `raw` |
| Units | Category `unit`; native `base` becomes `label`; unknown unit fields remain under adapter category extensions |
| Elimination and elimination value | Dimension `extension.nordicintel.elimination` / `elimination_value` |
| Category notes, roles and ordinary JSON-stat fields | Retained in their corresponding standard positions |
| Native extensions, PXWeb-only properties, mandatory notes and codelist references | Retained under the adapter namespace, including dimension extensions; no alternate codelist transformation is applied |
| v1 sampled flags and synthetic dimensions | Namespaced `harvest_metadata.sample_status`, `auxiliary_dimensions`, `auxiliary_roles`; migrated legacy enrichment may remain inside its complete `source_record` |
| Optional source values outside the shared profile | Exact original values retained in adapter `source_fields` under escaped JSON Pointer keys; required cube/category structure is never repaired by dropping fields |

`extension.metadata_harvest` is a migration input only; new documents use shared
identity and namespaced source evidence. Source timestamps are not harvest timestamps.
Operational start/finish times live in reports. A failed replacement never advances
its accepted source marker. Codelists are `{provider_code, language, codelist}` records,
with their native ID and full payload, and are never represented as datasets.

## Current state and failure semantics

`table-records/` stores current listings, `reports/` latest outcomes, and
`work/current.json` unfinished work only. Confirmed removal deletes current records
and unreferenced codelists. Incomplete discovery cannot authorize absence-based deletion.
Table and codelist failures are independent, reported explicitly and retried;
previous accepted data is retained during transient failures.

JSON object keys and record order are deterministic, LF-terminated UTF-8. Gzip
headers are deterministic, unchanged content preserves bytes, and writes are batched.
Replacement atomicity is per collection; the verified collector Git commit publishes
all output/state/errors together. Staging is adapter-scoped. A 100 MiB file gate
fails before publication; collections are never silently split or truncated.

## Repeatable conversion

```python
from metadata_harvest.migration import convert_collections

report = convert_collections(
    "data/pxweb_v1", "output/candidate/pxweb_v1", adapter="pxweb_v1"
)
```

Use a new candidate directory, with no unfinished source checkpoint. Conversion
validates every document, identities, dimensions, category order/counts and source
records, checks compressed sizes and proves the source manifest unchanged. A failed
conversion exposes no candidate. Inspect its `reports/conversion.json` before replacing
the corresponding published adapter directory; do not retain parallel legacy and
canonical collections. Conversion requires no provider traffic and proves mapping
integrity, not renewed source availability.

Schemas were adopted from `nordicintel-schemas` commit `77a7a46`; prepared `2.0.0`
was not published. Schema identifiers now belong to this repository, bundled in
both distributions. Schema revision provenance does not introduce dataset versions.

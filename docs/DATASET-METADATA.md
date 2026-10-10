> Transition reference: this documents the legacy native/observation shape. The authoritative published metadata contract is the [Pydantic StatDCAT profile](output-contract.md); new consumers use typed distributions.

# Legacy native metadata and observation mapping

[The schema](../harvesting/schemas/legacy-dataset-metadata.schema.json) defines a JSON-stat2
Dataset used by native parsing and transition retrieval, one document per language.
Published metadata instead uses multilingual StatDCAT datasets; see the
[field mapping](output-contract.md#field-dispositions). Its definitions are local and require no network access. Standard fields
follow the [published JSON-stat2 Dataset schema](https://json-stat.org/format/schema/2.0/dataset.json)
(v1.05, 2025-06-13); this profile adds constraints and namespaced metadata.

## Standard structure

Required fields are `version: "2.0"`, `class: "dataset"`, `value`, `id`,
`size`, `dimension`, `label` and `extension.nordicintel`.

`id` lists dimension codes in order; `size` lists their actual category counts in
that same order. Neither changes because observations are omitted. `value` must
be present: `[]` means metadata-only; a nonempty array or sparse object carries
observations. `id` is not a combined Dataset identifier. No extra mode flag is used.

Use standard `label`, `source`, `updated`, `note`, `role`
and `dimension` wherever applicable. Omit unknown standard fields instead of
emitting null (except `null` values for cells presented with a marker and `null`
entries in `status` arrays). Public identifiers and public API responses belong to
consuming systems. Standard tools can read the metadata; tools requiring data still
need observations.

## File-backed observations

When a provider offers observations only in files (XLS, XLSX, CSV, etc.), not through
a reachable observations API, the scraper must parse those files into this same
Dataset format **with observations** for NordicIntel to ingest, store and serve.
Metadata-only output remains available for the existing metadata workflow. The
schema filename and identity stay unchanged; this is not a public API contract.

Cell content follows the [observation content rules](#observation-content). Follow
dimension order in `id` and category order from each dimension's `category.index`;
the **last dimension varies fastest**. For `id: ["Region", "Time"]`, `size: [2, 2]`, regions `00, 01`
and times `2024, 2025`, positions are `(00, 2024)`, `(00, 2025)`, `(01, 2024)`,
`(01, 2025)`. A populated array has exactly `product(size)` cells.

Sparse `value` objects use those same flattened positions as decimal string keys,
not category codes: `"0"`, `"1"`, etc., without signs or leading zeros. Omitted
positions are cells the source has no data for. `{}` means the source has no data
for any cell, whereas `[]` specifically means metadata-only. `size` always describes
the full cube.

`status` accepts a string applying to all cells, a full-length array of strings/nulls,
or a sparse index-to-string object. It uses the same flattened positions as `value`;
a full-length status array also has entries for cells left out of a sparse `value`.
Null array entries and omitted map entries mean no flag supplied. Omit `status`
entirely for metadata-only output. Status codes retain provider meaning; this profile
does not impose a shared code vocabulary.

Keep `extension.nordicintel.data_url` pointing to the **original provider file**, not
NordicIntel's serving endpoint. `metadata_url` remains optional when the provider
has no separate metadata resource. All existing identity, language, namespace and
named URL rules apply unchanged.

## Observation content

These rules apply to every observation-bearing output: Dataset documents with
observations and adapter [retrieval fragments](RETRIEVAL.md#result). Observations
reflect exactly what the source provides.

1. **Provided values.** A cell the source gives a value for is included with that
   value. Parse numbers as numbers, keep zero as `0` and keep genuine textual
   observations as strings.
2. **Provided markers.** A cell the source presents with a marker instead of a
   value is included with value `null` and the marker verbatim in `status`, unless
   rule 3 applies. Explain known marker meanings in `note` or the appropriate
   provider/adapter namespace.
3. **Documented equivalents.** A marker becomes a specific value only when the
   implementation can point to provider documentation (an instruction, note, legend
   or description) that clearly states it, such as "–" standing for `0`. Those
   cells get that value and no `status` entry. Each substitution rule applied in a
   Dataset is recorded once, as one [substitution note](#substitution-notes).
   Adapters put it in the harvested Dataset document, because retrieval fragments
   carry no notes.
4. **No data from the source.** A cell the source provides nothing for (no row, no
   entry, a blank spreadsheet cell) is left out of both `value` and `status`. Never
   fill it with `0`, `null`, an estimate, or a value inferred from totals, other
   cells or other sources.
5. **Coverage.** `value` and `status` need not cover every cell. Use the sparse
   encoding whenever the source does not supply every cell; a dense array is only
   for sources that supply all cells. `size` and category indices still describe
   the full cube or selection.
6. **Consumers.** Treat a left-out cell as "the source has no data", never as zero
   or as a failure.

### Substitution notes

Record each applied substitution rule as one root `note` entry per Dataset.
Substitution notes are the last entries in `note`, written in the document language:

```text
en: Substituted marker: "<marker>" = <value>. Source: "<quote>" (<reference>)
sv: Ersatt markering: "<marker>" = <value>. Källa: "<citat>" (<referens>)
```

- `<marker>`: the source text exactly as it appears.
- `<value>`: the JSON number used in its place.
- `<quote>` / `<citat>`: the provider's defining statement, verbatim and in its
  original language. When the original text is long, use an excerpt containing
  the defining statement, with `…` marking omissions.
- `<reference>` / `<referens>`: absolute URL of the page or file containing the
  quote, followed by a location when needed, such as `, sheet "Teckenförklaring"`
  or `, p. 4`.

Example: `Substituted marker: "–" = 0. Source: "– Noll" (https://example.org/statistik/teckenforklaring)`

## Namespaces and identity

Every extension is an object whose keys identify namespaces:

| Namespace                   | Contents                                                 |
| --------------------------- | -------------------------------------------------------- |
| `extension.nordicintel`     | Shared fields defined by this schema                     |
| `extension.<provider_code>` | Additional metadata specific to a provider               |
| `extension.<adapter_type>`  | Additional metadata shared by providers using an adapter |

**Every namespace body accepts arbitrary additional properties**, including scalars,
null, arrays and nested objects. Namespace bodies themselves must be objects.
Defined fields retain their declared types and constraints. Use existing
JSON-stat2 or NordicIntel fields whenever they fit; never duplicate, alias or
bypass them through an extension. Reserve `nordicintel` for shared metadata.
Provider and adapter namespace names must be distinguishable; use the actual
provider code or established adapter type, not arbitrary per-record names.

Dataset `extension.nordicintel` requires `provider_code`, the provider's opaque,
case-sensitive `dataset_code`, and `language` (`sv` or `en`). Swedish and English
documents are independent. Preserve provider text and codes; never invent or
silently substitute translations. Harvesters do not construct a `dataset_id`.

Other shared Dataset fields remain optional inside that namespace:
`description`, `discontinued`, `official_statistics`, `time_unit`,
`first_period`, `last_period`, `next_release`, `subject`, `paths` and `contacts`.
Their existing meanings, nullable types and defaults remain defined in the schema.
Schema defaults annotate documents; they do not insert values.

## Attribution and dates

Root `source` is attribution reported by the provider and may name another
originating organization. It does not identify the provider.

Root `updated` records the last provider-reported modification, including data,
structure or metadata changes. Extract a valid `YYYY-MM-DD` calendar date without
requiring timezone conversion. Preserve useful original text as `updated_original`
in the provider or adapter namespace, for example
`extension.pxweb_v2.updated_original`. Omit root `updated` when unknown or when no
valid supported date can be extracted; do not invent one. The published schema
restricts date-only years to 1900–2099, which this profile follows.

`extension.nordicintel.next_release` remains permissive provider date/timestamp
text or null. `first_period` and `last_period` preserve provider period notation
and may be inferred by the implementation; `time_unit` describes granularity.
Unknown `official_statistics` or `discontinued` remains omitted or null.

## Dimensions and category metadata

Dimension `category.index` accepts an ordered code array or a code-to-position
map. Labels are optional when an index exists. A single category can instead be
identified by a one-entry label map. Category notes, hierarchies (`child`),
coordinates and units remain in their standard locations.

Unit `label`, `decimals`, `symbol` and `position` are optional. Preserve known
units without inventing precision; `position` places the symbol. Extra unit
metadata follows the category namespace convention below.

Shared dimension fields live in
`dimension.<id>.extension.nordicintel`: `elimination` (default false),
`elimination_value` (default null), optional attribution `source`, and permissive
provider update text `updated`. A supplied elimination value identifies an
existing category. The published Dataset schema does not allow dimension-level
`class`, `source` or `updated`, so they are not emitted as standard dimension fields.

There is no `category.extension`. Additional category metadata goes under the
enclosing dimension's namespace as `categories.<category_code>`, for example
`dimension.Region.extension.example.categories.01`. Each category entry is an
open object. Shared custom category fields use `nordicintel.categories`; provider
and adapter extras use their own namespace's `categories`. Keep standard category
fields in `category`, rather than repeating them in these objects.

## Provider URLs and additional resources

The following optional fields belong in `extension.nordicintel`. Each accepts
an absolute HTTP(S) URL or null when unknown; omission is also allowed.

| Field          | Purpose                                             |
| -------------- | --------------------------------------------------- |
| `source_url`   | Provider's human-readable Dataset page               |
| `doc_url`      | Provider's primary documentation or methodology      |
| `metadata_url` | Provider's metadata endpoint or file, for refetching  |
| `data_url`     | Provider's observation endpoint or file, for requests |

These URLs point **outward to the provider**. Current adapters supply both
`metadata_url` and `data_url` so downstream processes can reuse them directly
instead of reconstructing URLs from API conventions. Keep supplying them even
when the endpoint could be derived from other metadata. A URL does not encode
POST bodies or other retrieval configuration. The service passes both URLs verbatim
to the adapter's [retrieval function](RETRIEVAL.md), together with any values the
adapter lists in `REQUIRED_METADATA`; emit those values in every harvested document.

This model is for harvesters, scrapers and adapters, not the public API.
Public API data and metadata links point inward to that API's own endpoints;
they are separate from these provider URLs. Do not replace the named URL fields
with `href`, `link.describedby` or `link.related` entries.

Standard `href` and `link` remain available for additional resources. Each link
entry requires `href` and a descriptive `label`; include `type` when known.
Use `describedby` for additional documentation and `related` for other supporting
resources. Resolve relative URLs against the provider's page or endpoint.

Dimensions use the same `href` and `link` structures. A dimension resource can
set `extension.nordicintel.category_id` to an existing category of that dimension;
omit it for a dimension-wide resource. Dataset links concern the whole Dataset.
Custom explanatory text belongs in the entry's
`extension.nordicintel.description`. Other entry metadata uses provider/adapter
namespaces. Thematic path nodes and contacts remain custom NordicIntel structures
and keep their existing `url` attributes.

## Contacts

Within `extension.nordicintel.contacts`, always map parseable information to
`name`, `email`, `phone`, `organization`, `address` and `url`.
Extra properties are allowed only for information that cannot be parsed or mapped
into those attributes. Do not introduce aliases or raw duplicates. At least one
property is required; do not invent unknown details.

## Consumer checks

Enable format checking when validating. JSON Schema cannot enforce every
relationship: consumers check that `id` matches dimension keys, `size` matches
category counts, index-map positions are unique and contiguous from zero, and
category metadata and hierarchy references exist. Roles reference existing
dimensions, with at most one role per dimension. Check elimination values,
namespaced category entries and resource category IDs against their enclosing
dimension, and check namespace ownership and resource duplication.

For observation-bearing documents, check that dense `value` length equals
`product(size)`, sparse value/status indices are below that product, and a status
array has exactly that many entries. Check ordering against `id` and category
indices, not object insertion order. Scalar status applies to the entire cube.
These cross-field checks remain consumer responsibilities; schema validation
checks value/status types and sparse key syntax, and rejects `status` with `value: []`.

## Metadata-only example

One complete metadata-only document; further field-level examples live in the schema.

```json
{
  "version": "2.0",
  "class": "dataset",
  "label": "Befolkning efter region och år",
  "source": "Exempelmyndigheten",
  "updated": "2026-09-15",
  "note": ["Avser årets slut."],
  "id": ["Region", "Time"],
  "size": [2, 2],
  "role": { "geo": ["Region"], "time": ["Time"] },
  "dimension": {
    "Region": {
      "label": "Region",
      "category": {
        "index": ["00", "01"],
        "label": { "00": "Riket", "01": "Stockholms län" }
      },
      "link": {
        "related": [
          {
            "href": "https://example.org/regions/01/boundaries",
            "label": "Länsgränser",
            "extension": {
              "nordicintel": {
                "category_id": "01",
                "description": "Kartunderlag för regionen."
              }
            }
          }
        ]
      },
      "extension": {
        "nordicintel": {
          "elimination": true,
          "elimination_value": "00"
        },
        "example": {
          "categories": {
            "01": { "boundary_revision": "2025" }
          }
        }
      }
    },
    "Time": {
      "label": "År",
      "category": {
        "index": { "2024": 0, "2025": 1 },
        "label": { "2024": "2024", "2025": "2025" }
      }
    }
  },
  "value": [],
  "extension": {
    "nordicintel": {
      "provider_code": "example",
      "dataset_code": "POP01",
      "language": "sv",
      "source_url": "https://example.org/population",
      "doc_url": "https://example.org/population/methodology",
      "metadata_url": "https://example.org/api/POP01/metadata",
      "data_url": "https://example.org/api/POP01/data",
      "time_unit": "annual",
      "contacts": [
        {
          "organization": "Exempelmyndigheten",
          "email": "statistics@example.org",
          "opening_hours": "Vardagar 09–15"
        }
      ]
    },
    "pxweb_v2": {
      "updated_original": "2026-09-15 09:00:00"
    },
    "example": {
      "revision_policy": "Preliminary figures may be revised."
    }
  }
}
```

## Populated file-backed example

Complete output parsed from a provider CSV. Region varies slowest and Time fastest:
the four cells are `(00, 2024)`, `(00, 2025)`, `(01, 2024)`, `(01, 2025)`.
Zero is retained, a suppressed cell is null with a status flag, and a provisional
value keeps its flag. These example codes are explained in `note`, not imposed on
other providers. There is no separate metadata endpoint, so `metadata_url` is omitted.

```json
{
  "version": "2.0",
  "class": "dataset",
  "label": "Reported observations by region and year",
  "note": ["s = suppressed; p = provisional."],
  "id": ["Region", "Time"],
  "size": [2, 2],
  "dimension": {
    "Region": { "category": { "index": ["00", "01"] } },
    "Time": { "category": { "index": ["2024", "2025"] } }
  },
  "value": [0, null, 12.5, 18],
  "status": [null, "s", "p", null],
  "extension": {
    "nordicintel": {
      "provider_code": "example",
      "dataset_code": "FILE01",
      "language": "en",
      "data_url": "https://example.org/downloads/FILE01.csv"
    }
  }
}
```

## Sparse file-backed example

The same file, except that it has no entry for `(01, 2025)`: position `3` is left
out of both maps. The suppressed position `1` is present with `null` and its flag.
The cube still contains four cells. Either status encoding may accompany either
value encoding; use a scalar string only when a flag applies to every cell.

```json
{
  "version": "2.0",
  "class": "dataset",
  "label": "Reported observations by region and year",
  "note": ["s = suppressed; p = provisional."],
  "id": ["Region", "Time"],
  "size": [2, 2],
  "dimension": {
    "Region": { "category": { "index": ["00", "01"] } },
    "Time": { "category": { "index": ["2024", "2025"] } }
  },
  "value": { "0": 0, "1": null, "2": 12.5 },
  "status": { "1": "s", "2": "p" },
  "extension": {
    "nordicintel": {
      "provider_code": "example",
      "dataset_code": "FILE01",
      "language": "en",
      "data_url": "https://example.org/downloads/FILE01.csv"
    }
  }
}
```

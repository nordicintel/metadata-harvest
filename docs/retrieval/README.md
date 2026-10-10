# Standalone retrieval

Install with `pip install .`, then pass a distribution directly from compact catalog JSON-LD:

```python
from retrieval import retrieve_distribution

result = await retrieve_distribution(session, distribution, selection=selection)
```

`session` is caller-owned aiohttp. `distribution` is a dictionary from a catalog
record's `dcat:distribution` list. `selection` maps dimension codes to selected
category codes. Nested distributions need no separate context. Source identities
from profiles 1 and 2 are accepted without profile migration or context downloads.
The function preserves the input and interprets operational fields only.

The dispatcher is optional. Consumers can instead import an adapter directly,
for example `from retrieval.data_adapters.pxweb_v1 import retrieve`, using its existing
adapter-level arguments. Each adapter owns request construction, response parsing
and error handling; there is no mandatory common retrieval engine.

Consumer retrieval implementation and shared pure helpers live in root `retrieval/`.
Harvesting probes, coverage discovery and metadata inference stay in their
harvesting adapters, with their original clients and operational policy.
The wheel excludes harvesting, source inventories, Pydantic and compression tools.
Distribution name: `metadata-harvest`; Python import: `retrieval`.
All existing `nordicintel.retrieval` entry-point names remain available with their
adapter-level arguments; see the direct-adapter contract below.

Repository code lives in `apps/`. After `uv sync --locked --extra harvest`,
run its commands from the checkout using `uv run python -m apps.operations.harvest ...`.
No path environment changes are required. Repository callers holding a Pydantic
Distribution pass `distribution.to_jsonld()` to the same public retrieval function.
There are no retrieval forwarding modules or retrieval exports in `apps`.


Sessions, pacing, cancellation and retries stay under existing caller/adapter
policy. Codes retain their original spelling and order. Invalid operational
metadata and selections fail before requests. Returned observations retain the
existing coordinate and sparse-status format. Descriptive metadata is not required.

See [dependency inventory](dependencies.md) for ownership and verification.

## Function

```python
async def retrieve(
    session: aiohttp.ClientSession,
    *,
    provider_code: str,
    dataset_code: str,
    language: str,
    selection: Mapping[str, Sequence[str]],
    data_url: str | None,
    metadata_url: str | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]: ...
```

The service always passes every argument, taking values from the harvested Dataset
document for the requested `provider_code`, `dataset_code` and `language`:

| Argument        | Value                                                                         |
| --------------- | ----------------------------------------------------------------------------- |
| `session`       | The service's shared session, for centralized HTTP control                    |
| `provider_code` | `extension.nordicintel.provider_code`                                         |
| `dataset_code`  | `extension.nordicintel.dataset_code`                                          |
| `language`      | `extension.nordicintel.language` (`sv` or `en`)                               |
| `selection`     | Requested observations; see [Selection](#selection)                           |
| `data_url`      | `extension.nordicintel.data_url`, or `None` when absent                       |
| `metadata_url`  | `extension.nordicintel.metadata_url`, or `None` when absent                   |
| `metadata`      | Resolved `REQUIRED_METADATA`; see [Additional metadata](#additional-metadata) |

The URLs are passed verbatim. The service passes exactly these arguments for a given
schema collection version.

### Selection

`selection` maps dimension codes to nonempty, duplicate-free sequences of category
codes. Codes are strings, as in the Dataset document, even when the provider's API
uses numbers.

The service validates the selection against the catalog before calling: keys are
dimensions of the dataset and codes exist in that dimension's `category.index`.
Adapters need not repeat these checks.

An omitted dimension is eliminated. The service omits a dimension only when its
`extension.nordicintel.elimination` is true and `elimination_value` is null. When an
elimination value exists, the service selects that category instead. Every other
dimension is present. For an omitted dimension, the adapter returns the provider's
observations without it, typically an aggregate. When harvesting, mark a dimension
eliminable without a value only if the adapter can do this. Otherwise supply the
provider's total category as `elimination_value`, or leave `elimination` false.

### Additional metadata

When the standard arguments are not enough, the module containing the entry-point
target defines `REQUIRED_METADATA`: a tuple of static
[JSON Pointers](https://www.rfc-editor.org/rfc/rfc6901) into the Dataset document.
The service resolves each pointer against the same-language document and passes
`{pointer: value}` in `metadata`, omitting pointers that do not resolve. Without
`REQUIRED_METADATA`, `metadata` is `{}`.

```python
REQUIRED_METADATA = ("/extension/example", "/dimension")
```

Pointers cannot vary per dataset; point at an enclosing object when the needed values
live under dataset-specific keys. Harvesting must emit every targeted value the
retrieval function relies on, because the service has only the catalog document.

## Result

The function returns an observation fragment, not a complete Dataset: `id`, `size`,
`dimension` with category indices only, `value` and optional `status`. The service
copies these into the Dataset it serves and takes labels, units, notes, roles and
extensions from the catalog.

- `id` lists exactly the selected dimensions, in the order the adapter returns them.
- Each `dimension.<id>.category.index` lists exactly that dimension's selected codes,
  in the order the adapter returns them. Following the selection order is convenient
  but not required; the fragment makes the order explicit.
- `size` gives each dimension's category count in `id` order.
- `value` and `status` follow the
  [observation content rules](../model/dataset-metadata.md#observation-content) and the
  positional encoding of
  [file-backed observations](../model/dataset-metadata.md#file-backed-observations): last
  dimension varying fastest, provided values kept (zero stays `0`), marker cells as
  `null` with the marker in `status`, and cells the provider has no data for left
  out. An empty dense array is invalid; return `value: {}` when the provider has no
  data for the selection.

No other properties are allowed. `id`, `size` and the category indices always cover
the complete selection, even when `value` holds only some cells. Explain the
`status` codes retrieval can return, and record documented marker substitutions as
[substitution notes](../model/dataset-metadata.md#substitution-notes), in the harvested
documents.

### Consumer checks

The service validates the fragment against the schema and checks relationships that
JSON Schema cannot express. Adapters should test the same checks:

- `id` matches the `dimension` keys and the selection keys exactly.
- Each `size` entry equals the category count of the corresponding index. Index-map
  positions are unique and contiguous from zero.
- Each index contains exactly the selected codes for its dimension.
- Dense `value` and array `status` lengths equal `product(size)`; sparse indices are
  below it.

## Errors

Use built-in exceptions (or subclasses) so that the service can classify failures
without a shared package:

| Exception     | Meaning                                                                            |
| ------------- | ---------------------------------------------------------------------------------- |
| `ValueError`  | The provider rejects the selection, or it exceeds a limit the adapter cannot split |
| `LookupError` | The dataset no longer exists at the provider                                       |
| Any other     | Upstream or adapter failure, such as `aiohttp.ClientError` or `TimeoutError`       |

Let transport and unexpected errors propagate with their causes. Never return partial
or empty results for a failed retrieval, and do not catch `asyncio.CancelledError`.

## Execution

- Make every HTTP request through `session`. Do not create other sessions or clients,
  close the session, or change its shared state; per-request headers and parameters
  required by the provider are fine.
- Handle pagination and request splitting. Only the adapter knows the provider's
  limits; it may make several sequential or concurrent requests. The service may cap
  selection sizes itself; the adapter splits whatever the provider's limits require.
- Leave retries, backoff, caching, rate limiting and timeouts to the service.
- Do not write files, read environment variables or configuration files, or keep
  global state between calls. Log through `logging.getLogger(__name__)` without
  configuring logging.

## Example

```python
fragment = await retrieve(
    session,
    provider_code="example",
    dataset_code="POP01",
    language="sv",
    selection={"Region": ["00", "01"], "Time": ["2024", "2025"]},
    data_url="https://example.org/api/POP01/data",
    metadata_url="https://example.org/api/POP01/metadata",
    metadata={},
)
```

The provider returned three of the four cells: `(00, 2025)` carries the marker `..`,
and it has no data for `(01, 2025)`, which is left out.

```json
{
  "id": ["Region", "Time"],
  "size": [2, 2],
  "dimension": {
    "Region": { "category": { "index": ["00", "01"] } },
    "Time": { "category": { "index": ["2024", "2025"] } }
  },
  "value": { "0": 10, "1": null, "2": 0 },
  "status": { "1": ".." }
}
```

## Versioning

This repository owns the bundled schemas and their identifiers; consumers can pin a
repository commit or package release. The imported baseline is schema commit
`77a7a4665fea6f3c2be8f286d560b79317f08dd7`, whose prepared `2.0.0` was unpublished.
See [schema provenance](../../schemas/README.md).

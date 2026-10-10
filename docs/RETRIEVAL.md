> Transition reference: this documents the legacy native/observation shape. The authoritative published metadata contract is the [Pydantic StatDCAT profile](output-contract.md); new consumers use typed distributions.

# Legacy adapter retrieval contract

An API adapter makes one function separately installable and importable: the live
service calls it to fetch selected observations for a dataset whose metadata has
already been harvested. This document is the contract for that function: packaging,
discovery, inputs, result and errors. [The result schema](../src/metadata_harvest/schemas/retrieval-result.schema.json)
validates the returned structure.

New callers use `retrieve_distribution(session, distribution, selection=...)` as
shown in the [typed retrieval guide](retrieval-guide.md). Dispatch follows the
distribution adapter, so one owner can expose multiple APIs. The provider entry
points and metadata pointers below document the compatibility interface retained
for one transition release, using [legacy native documents](DATASET-METADATA.md).

## Packaging and discovery

All implemented adapters share the `metadata-harvest` distribution. A plain install, without extras, provides the
retrieval module. Its runtime dependencies are `aiohttp`, Pydantic v2 and the standard library. Harvesting-only
dependencies belong under the `harvest` extra. The retrieval module never imports
harvesting modules or harvesting-only dependencies; harvesting code may import
retrieval code.

The service discovers retrieval functions through the `nordicintel.retrieval`
entry-point group. Each entry name is a supported `provider_code` and each target is
the async retrieval function. An adapter for a shared API type lists every provider
it supports. Distribution, package, module and function names are the adapter's choice.

```toml
[project]
dependencies = ["aiohttp>=3.9,<4", "pydantic>=2.12,<3"]

[project.optional-dependencies]
harvest = ["..."]

[project.entry-points."nordicintel.retrieval"]
example = "example_adapter.retrieval:retrieve"
example_region = "example_adapter.retrieval:retrieve"
```

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
  [observation content rules](DATASET-METADATA.md#observation-content) and the
  positional encoding of
  [file-backed observations](DATASET-METADATA.md#file-backed-observations): last
  dimension varying fastest, provided values kept (zero stays `0`), marker cells as
  `null` with the marker in `status`, and cells the provider has no data for left
  out. An empty dense array is invalid; return `value: {}` when the provider has no
  data for the selection.

No other properties are allowed. `id`, `size` and the category indices always cover
the complete selection, even when `value` holds only some cells. Explain the
`status` codes retrieval can return, and record documented marker substitutions as
[substitution notes](DATASET-METADATA.md#substitution-notes), in the harvested
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
See [schema provenance](../src/metadata_harvest/schemas/README.md).

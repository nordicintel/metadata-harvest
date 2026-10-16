# Observation retrieval

`pip install .` installs the `metadata-harvest` distribution with the `retrieval`
namespace, requiring Python 3.13+ and `aiohttp`. Repository applications, Pydantic,
inventories, compression and harvesting clients are excluded.

## Worker documents

```python
from retrieval import retrieve_distribution

result = await retrieve_distribution(session, document, selection=selection)
```

Pass a decoded worker document: one line of an accepted collection under
`data/<adapter>/metadata/`. The dispatcher reads the adapter, provider, dataset
code, language and URLs from `extension.nordicintel`, the selectable categories from
`dimension`, and the adapter's operational metadata from the document's own
extension namespace.

`selection` maps dimension codes to nonempty, duplicate-free sequences of string
category codes. Preserve spelling and leading zeroes. The dispatcher checks codes
and operational metadata before requesting data. Follow the distribution's
elimination settings: select an explicit elimination category when defined; omit
a dimension only where the adapter supports elimination without a selected value.
The input mapping is not mutated. Descriptive fields are not required.

## Direct adapters and dependencies

The dispatcher is optional. Each module in `retrieval/data_adapters/` owns its
consumer implementation and supports this keyword interface:

```python
from retrieval.data_adapters.pxweb_v1 import retrieve

result = await retrieve(
    session,
    provider_code=source_alias,
    dataset_code=native_code,
    language=language,
    selection=selection,
    data_url=data_url,
    metadata_url=metadata_url,
    metadata=operational_metadata,
)
```

Direct calls require the adapter-specific pointer-keyed `metadata` dictionary listed
by that module's `REQUIRED_METADATA`; the pointers address the worker document.
Consumers with a document should use the dispatcher, which resolves them. The
`nordicintel.retrieval` entry points target the same direct functions.

| Implementations | Runtime support |
| --- | --- |
| `pxweb_v1`, `pxweb_v2` | `_selection`: request splitting and coordinate-aligned assembly |
| `kolada` | `_semantics`: category ordering and cube validation |
| `uka` | `_uka_export`: CSV parsing and protocol constants |
| `dataanalys`, `entryscape`, `forsakringskassan`, `riksbank`, `sdb`, `smhi_emissions`, `tillvaxtverket`, `trafa` | Adapter-local request/response code, standard library and `aiohttp` |
| `distribution` | Document validation, selection checks, explicit adapter lookup |

Harvesting may import identical pure helpers; consumer functions never import
repository workflows. Exact required fields and request caps live beside each
adapter implementation, avoiding a second hand-maintained operational schema.

## Result

Returns `id`, `size`, `dimension` (category indices), `value`, and optional `status`.
Labels, units and descriptive metadata come from the worker document. Dimension order follows
`id`; category indices define order explicitly; the last dimension varies fastest.

- `id`, dimension keys and requested axes agree; `size` matches category counts.
- Dense values contain `product(size)` cells. Sparse maps use flattened decimal
  indices (`"0"`, `"1"`, …), never category codes, within that product.
- Preserve provided numbers, zero and textual values. Provider markers become
  `null` with their meaning retained in `status`. Only documented substitutions
  may change a marker to a value; record their justification in harvested notes.
- Missing cells are absent, not invented zeroes or nulls. Empty observations use
  `value: {}`; `value: []` is reserved for metadata-only worker documents.
- Status may be a scalar string, a full-length string/null array or a sparse map;
  flags retain provider-specific meaning.

For two regions and two periods, `value: {"0": 10, "1": null, "2": 0}` with
`status: {"1": ".."}` preserves a marked cell, a real zero and an absent fourth cell.
EntryScape sums matching rows; do not treat sums of shares or averages as meaningful
without checking `cells_unique` and source measure semantics. SMHI-provided zeroes
cannot distinguish missing combinations from genuine zeroes.

## Sessions, errors and verification

The caller owns the aiohttp session, timeouts and retry policy. Adapters use that
session and handle their protocol's pagination/splitting without closing it or
changing shared state. Errors propagate: `ValueError` for invalid/over-cap selections,
`LookupError` for unavailable datasets, and transport or adapter errors for failed
requests. Cancellation propagates; failed requests must not become partial successes.

Run `uv run pytest tests/retrieval` for offline adapter and dispatcher fixtures.
After building, `uv run python scripts/checks/check_distribution.py` verifies an
isolated installation and loads every entry point. These checks do not establish
live provider availability; scripts under `scripts/live/` are explicitly separate.

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
for example `from retrieval.adapters.pxweb_v1 import retrieve`, using its existing
adapter-level arguments. Each adapter owns request construction, response parsing
and error handling; there is no mandatory common retrieval engine.

Consumer retrieval implementation and shared pure helpers live in root `retrieval/`.
Harvesting probes, coverage discovery and metadata inference stay in their
harvesting adapters, with their original clients and operational policy.
The wheel excludes harvesting, source inventories, Pydantic and compression tools.
Distribution name: `metadata-harvest`; Python import: `retrieval`.
All existing `nordicintel.retrieval` entry-point names remain available with their
adapter-level arguments; see [the contract](RETRIEVAL.md).

Repository code lives in root `harvesting/`. After `uv sync --locked --extra harvest`,
run its commands from the checkout using `uv run python -m harvesting.runner ...`.
No path environment changes are required. Repository callers holding a Pydantic
Distribution pass `distribution.to_jsonld()` to the same public retrieval function.
There are no retrieval forwarding modules or retrieval exports in `harvesting`.


Sessions, pacing, cancellation and retries stay under existing caller/adapter
policy. Codes retain their original spelling and order. Invalid operational
metadata and selections fail before requests. Returned observations retain the
existing coordinate and sparse-status format. Descriptive metadata is not required.

See [dependency inventory](retrieval-dependencies.md) for ownership and verification.

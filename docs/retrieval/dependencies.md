# Retrieval dependency inventory

The sole distributed code is the root `retrieval/` folder. The distribution name
remains `metadata-harvest`; root packaging and one lockfile are retained.

| Adapter | Direct imports | Required adapter metadata |
|---|---|---|
| dataanalys | `aiohttp`, `asyncio`, `collections.abc`, `json`, `logging`, `math`, `re`, `typing` | `/id`, `/extension/socialstyrelsen_analys` |
| entrysoft | `aiohttp`, `collections.abc`, `math`, `re`, `typing` | `/id`, `/dimension`, `/extension/entrysoft` |
| forsakringskassan | `aiohttp`, `collections.abc`, `logging`, `math`, `typing`, `urllib.parse` | `/id`, `/extension/forsakringskassan` |
| kolada | `retrieval._semantics`, `urllib.parse` | `/dimension`, `/extension/kolada` |
| pxweb_v1 | `aiohttp`, `collections.abc`, `retrieval._selection`, `typing` | `/extension/pxweb_v1`, `/dimension` |
| pxweb_v2 | `aiohttp`, `collections.abc`, `retrieval._selection`, `typing`, `urllib.parse` | `/extension/pxweb_v2`, `/dimension` |
| riksbank | `aiohttp`, `collections.abc`, `datetime`, `logging`, `typing`, `urllib.parse` | `/id`, `/extension/riksbank` |
| sdb | `aiohttp`, `asyncio`, `collections.abc`, `itertools`, `logging`, `math`, `re`, `typing`, `urllib.parse` | `/extension/socialstyrelsen/result_dimensions` |
| smhi_emissions | `aiohttp`, `collections.abc`, `logging`, `math`, `re`, `typing` | `/id`, `/extension/smhi_emissions` |
| tillvaxtverket | `aiohttp`, `collections.abc`, `logging`, `math`, `typing`, `urllib.parse` | `/id`, `/extension/tillvaxtverket` |
| trafa | `aiohttp`, `asyncio`, `collections.abc`, `logging`, `math`, `re`, `typing`, `urllib.parse` | `/id`, `/extension/trafa` |
| uka | `aiohttp`, `asyncio`, `collections.abc`, `logging`, `math`, `retrieval._uka_export`, `typing`, `urllib.parse` | `/role` |

## Shared implementation and direction

- `_selection.py`: PXWeb request splitting and coordinate assembly; imports only
  standard library, aiohttp and `_semantics`.
- `_semantics.py`: category ordering, hierarchy and cube validation; standard library only.
- `_uka_export.py`: CSV decoding, parsing and protocol constants; standard library only.
- `distribution.py`: validates operational catalog fields and selects one of the
  twelve built-in adapters. Standard library plus `_semantics`; no model import.
- Harvesting may import identical pure retrieval helpers; retrieval never imports repository modules.
- Pydantic, Zstandard, inventory resolution, discovery, models, migrations,
  publication and Git operations remain repository-only.

All 85 existing `nordicintel.retrieval` entry-point names in root `pyproject.toml`
are preserved and target `retrieval.data_adapters.<adapter>:retrieve`. Routing aliases
are provider routing entry points, not a bundled organisation/catalog inventory.
Harvesting imports shared pure helpers directly from their defining modules.
Retrieval functions are exposed only through `retrieval`; no old forwarding
modules, adapter wrappers or typed harvesting bridge remain.

## Verification

Existing adapter fixtures exercise the moved implementations, including both
Kolada scopes, splitting, sparse coordinates, error handling and cancellation.
Catalog projection fixtures compare required adapter inputs with the previous
model-based projection for all twelve adapters and both source-identity spellings.
Transport fixtures additionally execute every real adapter directly and through
the dispatcher, comparing HTTP requests and observation results, including both
Kolada scopes. Harvesting client/source/workflow tests fail if they invoke a
consumer retrieval function or dispatcher.
An isolated wheel check loads every entry point, runs a fixture-backed PXWeb
request and confirms no repository modules or harvesting dependencies are present.
These checks do not contact live providers or rebuild production output.

## Harvesting continuity

The repository application moved to `apps/`, with imports, scripts,
resource paths and Actions commands updated together. `uv sync --locked --extra
harvest` still supplies its environment. All eleven client modules were inspected:
they implement harvesting transport, retry/pacing and discovery/probe policy, not
the observation retrieval entry points, and remain in harvesting. Required UKÄ
export parsing and cube helpers live in retrieval and are imported directly.
No accepted data, checkpoints or production archives were moved or rebuilt.

## Ownership rule

Each adapter owns its retrieval function; a common retrieval implementation is
not required. The catalog dispatcher is optional consumer routing, not a mandatory
harvesting or adapter interface. Harvest-specific probes remain with the harvesting
adapter even when they read observations. See the authoritative
[adapter retrieval ownership rules](../architecture.md#adapter-retrieval-ownership).

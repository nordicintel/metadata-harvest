# Standalone PXWeb retrieval

A plain installation of this one distribution provides both retrieval modules and
`nordicintel.retrieval` provider entry points using only `aiohttp` and the standard
library. Install `[harvest]` only for harvesting or schema validation.

The following example consumes an already loaded, same-language published Dataset;
it requests the first base category of every dimension and does not write observations:

```python
from importlib import import_module
from importlib.metadata import entry_points
import aiohttp


async def first_cell(document):
    identity = document["extension"]["nordicintel"]
    retrieve = entry_points(
        group="nordicintel.retrieval", name=identity["provider_code"]
    )[0].load()
    required = import_module(retrieve.__module__).REQUIRED_METADATA
    resolved = {}
    for pointer in required:
        value = document
        for key in pointer.strip("/").split("/"):
            value = value[key.replace("~1", "/").replace("~0", "~")]
        resolved[pointer] = value
    selection = {}
    for code in document["id"]:
        index = document["dimension"][code]["category"]["index"]
        ordered = sorted(index, key=index.get) if isinstance(index, dict) else index
        selection[code] = ordered[:1]
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as session:
        return await retrieve(
            session,
            provider_code=identity["provider_code"],
            dataset_code=identity["dataset_code"],
            language=identity["language"],
            selection=selection,
            data_url=identity.get("data_url"),
            metadata_url=identity.get("metadata_url"),
            metadata=resolved,
        )
```

Applications should reuse their own session and supply rate limiting, retries,
timeouts and caching. The adapter never changes or closes it. Both adapters require
`/dimension` and `/extension/<adapter>`; harvesting supplies the base categories and
provider cell limit. The fallback split limit is 10,000 cells for migrated metadata
without an advertised limit. Requests can be split further after a size rejection.

V1 uses `query`/`item` selections; v2 uses the official
[POST selection specification](https://www.pxtools.net/PxWebApi/documentation/user-guide/#query-in-the-post-body).
Results are assembled by coordinates, preserving caller order, zero, null, sparse
absence and provider flags. Only `id`, `size`, category indices, `value` and optional
`status` are returned. No labels or metadata are synthesized into the fragment.
Published base categories are used; alternate codelist transformations are excluded.

Rejected selections raise `ValueError`; confirmed disappearance raises `LookupError`;
transport/server errors and cancellation propagate. Never interpret a failed request
as an empty observation result. See the full [retrieval contract](RETRIEVAL.md).

Sundsvall first performs a metadata GET through the same caller session to establish
its anonymous gateway cookies; otherwise the gateway redirects the first POST
into a GET and drops the selection. V2 normalizes existing `lang` and `outputFormat`
parameters so published URLs never produce duplicate query parameters.

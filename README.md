# metadata-harvest

NordicIntel metadata harvesting from Swedish statistics providers, with a
standalone Python library for retrieving observations through twelve adapters.

## Retrieve observations

Python 3.13+. `pip install .` installs only `retrieval/`, under the distribution
name `metadata-harvest`. Its only third-party runtime dependency is `aiohttp`.

```python
from retrieval import retrieve_distribution

# document is one decoded line of data/<adapter>/metadata/*.jsonl.gz;
# session is a caller-owned aiohttp session.
selection = {
    code: list(axis["category"]["index"])[:1]
    for code, axis in document["dimension"].items()
}
result = await retrieve_distribution(session, document, selection=selection)
```

See [retrieval usage](docs/retrieval/README.md) for selections, direct adapter calls,
result semantics and errors.

## Work in the repository

```sh
uv sync --locked --extra harvest
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

Browser tests need `uv run playwright install chromium` (`--with-deps` on Linux).
`apps/` contains repository applications and
is excluded from installation. Run commands from the checkout using
`uv run python -m apps.operations.harvest --help`.

Harvests store provider responses under `data/<adapter>/source/`; a manual parse
builds the accepted collections under `data/<adapter>/metadata/` from them.

Start with the [documentation index](docs/README.md),
[architecture](docs/architecture.md), or [source inventory](docs/configuration/inventory.md).
The schema seeds and their [provenance](schema/README.md) live under `schema/`.

# metadata-harvest

NordicIntel metadata harvesting and offline StatDCAT catalog construction, with a
standalone Python library for retrieving observations from twelve adapter types.

## Retrieve observations

Python 3.13+. `pip install .` installs only `retrieval/`, under the distribution
name `metadata-harvest`. Its only third-party runtime dependency is `aiohttp`.

```python
from retrieval import retrieve_distribution

# record is decoded catalog JSONL; session is a caller-owned aiohttp session.
distribution = next(d for d in record["dcat:distribution"] if d.get("ni:retrieval"))
selection = {
    axis["code"]: axis["categories"][:1]
    for axis in distribution["ni:retrieval"]["dimensions"]
}
result = await retrieve_distribution(session, distribution, selection=selection)
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

Accepted metadata lives in `data/<adapter>/`. Catalog construction consumes it
offline, enriching matches from the local portal snapshot while retaining unmatched
datasets. Building a candidate and promoting production output are separate actions.

Start with the [documentation index](docs/README.md),
[architecture](docs/architecture.md), or [source inventory](docs/configuration/inventory.md).
Supporting schemas retain their [licence and provenance](schemas/README.md).

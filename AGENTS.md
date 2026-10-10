# Repository Guidelines

## Project Structure & Module Organization

- `retrieval/` is the only installable library; consumer implementations live in `retrieval/data_adapters/`. Its runtime dependencies are its own modules, `aiohttp`, and the standard library.
- `apps/` contains repository applications: model, harvest, catalog, portal, publication, migrations, and operations. Harvesting owns metadata collection, probes, retries, and checkpoints.
- `config/` holds authoritative inventories and settings; `schemas/` holds generated contracts and supporting validation resources. Pydantic models in `apps/model/` are authoritative.
- `data/` contains accepted metadata and production artifacts. `docs/` and `tests/` follow application responsibilities. Developer scripts are grouped under `scripts/{checks,generate,live,migrations}/`.
- Before changing module boundaries, read [architecture guidance](docs/architecture.md). Keep pure model helpers independent of workflows and persistence.

## Build, Test, and Development Commands

Run from the checkout with Python 3.13+:

```sh
uv sync --locked --extra harvest              # Install development dependencies
uv run ruff check .                          # Lint
uv run ruff format --check .                 # Check formatting
uv run pytest tests/retrieval                # Focused offline tests
uv run pytest -m "not slow"                 # Fast suite run on every push
uv run pytest                               # Complete offline suite (slow part runs nightly)
uv build                                    # Build retrieval-only distributions
uv run python scripts/checks/check_distribution.py # Verify isolated installation
```

Use `uv run python -m apps.operations.harvest --help` for harvesting options. After model/configuration changes, run both generators in `scripts/generate/` with `--check`; after schedule changes, run `uv run python -m apps.operations.scheduling sync --check`. Browser work requires Playwright Chromium.

## Coding Style & Naming Conventions

Use four-space indentation, Ruff formatting (88 columns), `snake_case` functions/modules, and `PascalCase` classes. Specify UTF-8 for text files. Preserve opaque identifiers, ordering, meaningful empty values, zeroes, and `False`.

## Testing Guidelines

Use pytest with `test_*.py` files and `test_*` functions under the owning responsibility. Verify actual requests/results and failure retention. Run affected tests first; distinguish fixture evidence from explicit live checks under `scripts/live/`. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Follow history’s short imperative subjects, such as “Organize developer scripts.” Describe the problem, resulting behavior, and verification in PRs; link relevant issues. Keep unrelated edits separate. Refactors must preserve accepted data and production artifacts; publication is a separate explicit operation.

## Agent Instructions

When delegation is requested, specify each subagent’s model and reasoning effort explicitly: GPT-6.1 Sol at Medium or cheaper; use GPT-6 Luna for very simple tasks.

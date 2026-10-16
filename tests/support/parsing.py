"""Parse an adapter's source files in tests, replacing the old document loaders."""

import importlib
from pathlib import Path

from apps.configuration import providers_for_adapter
from apps.harvest.source_store import SourceStore
from apps.parsing.build import load_state


def documents(
    adapter: str,
    root: Path,
    providers: list[dict] | None = None,
    *,
    portal: Path | None = None,
    include_withheld: bool = False,
) -> dict[str, dict]:
    """Parsed worker documents of ``root`` (``data/<adapter>``) keyed ``<language>/<dataset_code>``.

    Mapping failures raise so a test sees the cause instead of a missing key.
    """
    root = Path(root)
    module = importlib.import_module(f"apps.harvest.metadata_adapters.{adapter}.parse")
    store = SourceStore(root / "source", adapter)
    state = load_state(root)
    providers = providers if providers is not None else providers_for_adapter(adapter)
    withheld = set(module.withheld_keys(state))
    result = {}
    for key, item in module.documents(store, state, providers, portal=portal):
        if key in withheld and not include_withheld:
            continue
        if isinstance(item, Exception):
            raise item
        shared = item["extension"]["nordicintel"]
        result[f"{shared['language']}/{shared['dataset_code']}"] = item
    return result

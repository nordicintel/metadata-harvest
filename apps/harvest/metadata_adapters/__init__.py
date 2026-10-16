"""The metadata adapters, by name."""

import importlib
import importlib.util

ADAPTERS = (
    "pxweb_v1",
    "pxweb_v2",
    "kolada",
    "uka",
    "sdb",
    "trafa",
    "entryscape",
    "smhi_emissions",
    "forsakringskassan",
    "riksbank",
    "dataanalys",
    "tillvaxtverket",
)


def workflow_file(name: str) -> str:
    """The GitHub workflow that runs the adapter's harvest."""
    return (
        "harvest.yml" if name == "pxweb_v1" else f"harvest-{name.replace('_', '-')}.yml"
    )


def load(name: str):
    """The adapter's ``harvest`` module, or None while it still has a workflow.py."""
    if name not in ADAPTERS:
        raise ValueError(f"Unknown adapter: {name}")
    module = f"apps.harvest.metadata_adapters.{name}.harvest"
    return importlib.import_module(module) if importlib.util.find_spec(module) else None

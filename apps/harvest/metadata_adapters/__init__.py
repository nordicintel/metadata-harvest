"""The metadata adapters, by name."""

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

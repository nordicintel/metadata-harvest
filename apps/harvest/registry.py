"""Explicit built-in adapter inventory; not a runtime plugin framework."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Adapter:
    name: str
    available: bool
    workflow_file: str | None = None
    commands: tuple[str, ...] = ()
    preparation: str | None = None


ADAPTERS = {
    "pxweb_v1": Adapter(
        "pxweb_v1",
        True,
        "harvest.yml",
        (
            "prepare",
            "refresh",
            "collect-refresh",
            "build",
            "collect-metadata",
            "publish",
            "continue",
        ),
    ),
    "pxweb_v2": Adapter(
        "pxweb_v2",
        True,
        "harvest-pxweb-v2.yml",
        ("run", "collect", "publish"),
    ),
    "kolada": Adapter(
        "kolada",
        True,
        "harvest-kolada.yml",
        ("run", "collect", "publish"),
    ),
    "uka": Adapter(
        "uka",
        True,
        "harvest-uka.yml",
        ("run", "collect", "publish"),
    ),
    "sdb": Adapter(
        "sdb",
        True,
        "harvest-sdb.yml",
        ("run", "collect", "publish"),
    ),
    "trafa": Adapter(
        "trafa",
        True,
        "harvest-trafa.yml",
        ("run", "collect", "publish"),
    ),
    "entrysoft": Adapter(
        "entrysoft",
        True,
        "harvest-entrysoft.yml",
        ("run", "collect", "publish"),
    ),
    "smhi_emissions": Adapter(
        "smhi_emissions",
        True,
        "harvest-smhi-emissions.yml",
        ("run", "collect", "publish"),
    ),
    "forsakringskassan": Adapter(
        "forsakringskassan",
        True,
        "harvest-forsakringskassan.yml",
        ("run", "collect", "publish"),
    ),
    "riksbank": Adapter(
        "riksbank",
        True,
        "harvest-riksbank.yml",
        ("run", "collect", "publish"),
    ),
    "dataanalys": Adapter(
        "dataanalys",
        True,
        "harvest-dataanalys.yml",
        ("run", "collect", "publish"),
    ),
    "tillvaxtverket": Adapter(
        "tillvaxtverket",
        True,
        "harvest-tillvaxtverket.yml",
        ("run", "collect", "publish"),
    ),
}


def get_adapter(name: str, *, require_available: bool = True) -> Adapter:
    """Resolve an adapter and reject prepared-but-unimplemented capabilities."""
    if name not in ADAPTERS:
        raise ValueError(f"Unknown adapter: {name}")
    adapter = ADAPTERS[name]
    if require_available and not adapter.available:
        raise NotImplementedError(
            f"{name} is not implemented here; see {adapter.preparation}"
        )
    return adapter

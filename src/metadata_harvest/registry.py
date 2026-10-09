"""Explicit built-in adapter inventory; not a runtime plugin framework."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Adapter:
    name: str
    available: bool
    configuration: str | None = None
    workflow_file: str | None = None
    commands: tuple[str, ...] = ()
    preparation: str | None = None


ADAPTERS = {
    "pxweb_v1": Adapter(
        "pxweb_v1",
        True,
        "providers.json",
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
        "providers.pxweb_v2.json",
        "harvest-pxweb-v2.yml",
        ("run", "collect", "publish", "continue"),
    ),
    "kolada": Adapter(
        "kolada",
        True,
        "providers.kolada.json",
        "harvest-kolada.yml",
        ("run", "collect", "publish", "continue"),
    ),
    **{
        name: Adapter(name, False, preparation=f"docs/adapters/{name}.md")
        for name in ("sdb", "trafa", "rowstore", "uka")
    },
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

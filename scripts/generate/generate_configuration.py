"""Generate the service inventory from the registry; --check verifies it is current."""

import sys

import registry
from apps.paths import ROOT


def artifacts():
    loaded = registry.load()
    lines = [
        "# Service inventory",
        "",
        "Generated from the three configuration files in config/; do not edit by hand.",
        "",
        "| Owner | Adapter | Source code | Endpoint |",
        "| --- | --- | --- | --- |",
    ]
    for adapter in loaded.adapters:
        for binding in loaded.bindings(adapter):
            owner = loaded.owner(binding)
            lines.append(
                f"| {next(iter(owner.name.values()))[0] if owner else ''} | "
                f"`{binding.adapter}` | `{binding.source_code}` | {binding.endpoint_url} |"
            )
    lines.append("")
    yield ROOT / "docs/configuration/inventory.md", "\n".join(lines)


if __name__ == "__main__":
    for path, content in artifacts():
        if "--check" in sys.argv:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Generated artifact is stale: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

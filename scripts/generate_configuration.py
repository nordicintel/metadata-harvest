"""Generate package snapshots, configuration schema and source inventory."""

import json
import sys
from pathlib import Path

from harvesting.configuration import SourceConfiguration, load_configuration

ROOT = Path(__file__).resolve().parents[1]


def artifacts():
    configuration = load_configuration(ROOT)
    for name in ("adapters.json", "organisations.json", "catalogs.json"):
        yield (
            ROOT / "harvesting/configuration" / name,
            (ROOT / name).read_text(encoding="utf-8"),
        )
    schema = SourceConfiguration.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    yield (
        ROOT / "harvesting/schemas/source-configuration.schema.json",
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
    )
    reference = [
        "# Configuration model reference",
        "",
        "Generated from the configuration Pydantic models; do not edit by hand.",
        "",
        "Configuration fields are internal construction values. Organisation vocabulary aliases are standard RDF properties; published resource mappings are described in [configuration.md](configuration.md). JSON-valued extensions are accepted without changing these models.",
        "",
    ]
    for name, definition in sorted(
        {"SourceConfiguration": schema, **schema.get("$defs", {})}.items()
    ):
        if not definition.get("properties"):
            continue
        reference += [
            f"## {name}",
            "",
            "| Field | Required | Definition |",
            "| --- | --- | --- |",
        ]
        for field, item in definition["properties"].items():
            detail = json.dumps(item, ensure_ascii=False, sort_keys=True).replace(
                "|", "&#124;"
            )
            reference.append(
                f"| `{field}` | {field in definition.get('required', [])} | `{detail}` |"
            )
        reference.append("")
    yield ROOT / "docs/configuration-reference.md", "\n".join(reference)
    lines = [
        "# Organisation, catalog and concrete service inventory",
        "",
        "Generated from the three root configuration files; do not edit this inventory directly.",
        "",
        "Organisations are independent entities. Publisher is a relationship to a catalog or service. The source code is a compatibility routing alias, not an organisation identifier.",
        "",
        "| Catalog | Publisher | Adapter | Source alias | Service endpoint |",
        "| --- | --- | --- | --- | --- |",
    ]
    for catalog in configuration.catalogs.values():
        for identity in catalog.services:
            service = configuration.services[identity]
            label = next(iter(catalog.title.values()))[0]
            lines.append(
                f"| {label} | `{catalog.publisher or ''}` | `{service.adapter}` | `{service.source_code}` | {service.endpoint_url[0]} |"
            )
    lines += [
        "",
        "Shared hosting retains catalog-specific EntryStore contexts and original discovery origins. Records without supported attribution remain retained with findings; hosts never become invented organisations.",
        "",
    ]
    yield ROOT / "docs/source-inventory.md", "\n".join(lines)


if __name__ == "__main__":
    for path, content in artifacts():
        if "--check" in sys.argv:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Generated artifact is stale: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

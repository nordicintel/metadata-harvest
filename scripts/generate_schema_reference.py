"""Generate published JSON Schemas and field documentation from Pydantic v2."""

import json
import sys
from pathlib import Path

from harvesting.models import CodeList, Dataset, HarvestContribution

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "dataset-metadata": Dataset,
    "codelist": CodeList,
    "harvest-contribution": HarvestContribution,
}


def artifacts():
    schemas = {}
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True, mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = (
            f"https://github.com/nordicintel/metadata-harvest/schemas/{name}.schema.json"
        )
        schema.setdefault("properties", {})["@context"] = {"type": "object"}
        schemas[name] = schema
        yield (
            ROOT / f"harvesting/schemas/{name}.schema.json",
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        )
    lines = [
        "# Metadata model reference",
        "",
        "Generated from [Pydantic models](../harvesting/models.py); do not edit by hand.",
        "",
        "NordicIntel profile 2.0.0 is based on StatDCAT-AP 3.0.0. Vocabulary aliases are the serialized property names. Descriptive completeness is diagnostic; retrieval identities and structures are validated.",
        "",
        "Every source-bearing model accepts JSON-valued extras. Serialization places unfamiliar properties in `ni:additionalAttributes` under the `custom` namespace; adapters use their source namespace. This property is explicitly JSON-valued in the inline JSON-LD context.",
        "",
    ]
    definitions = {
        "Dataset": schemas["dataset-metadata"],
        **schemas["harvest-contribution"].get("$defs", {}),
    }
    for name, schema in sorted(definitions.items()):
        if not schema.get("properties"):
            continue
        lines.extend(
            [
                f"## {name}",
                "",
                "| Property | Required | Definition |",
                "| --- | --- | --- |",
            ]
        )
        for key, definition in schema["properties"].items():
            text = json.dumps(definition, ensure_ascii=False, sort_keys=True).replace(
                "|", "&#124;"
            )
            lines.append(
                f"| `{key}` | {key in schema.get('required', [])} | `{text}` |"
            )
        lines.append("")
    yield ROOT / "docs/DATASET-METADATA-REFERENCE.md", "\n".join(lines)


if __name__ == "__main__":
    for path, content in artifacts():
        if "--check" in sys.argv:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Generated artifact is stale: {path}")
        else:
            path.write_text(content, encoding="utf-8", newline="\n")

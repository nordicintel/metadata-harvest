"""Generate the Dataset field reference from the packaged authoritative schema.

Renderer adapted from nordicintel/nordicintel-schemas 77a7a46 (Apache-2.0).
"""

import html
import json
import sys
from pathlib import Path

SCHEMA = "src/metadata_harvest/schemas/dataset-metadata.schema.json"


def cell(value: str) -> str:
    return html.escape(value, quote=False).replace("|", "&#124;").replace("\n", "<br>")


def render(schema: dict) -> str:
    """Document reachable properties; expand scalar refs, link shared object shapes once."""
    sections = [("Dataset", schema, "jstat")]
    references = {}

    def resolve(node: dict, seen: tuple = ()) -> dict:
        if not isinstance(node, dict):
            return {} if node else {"description": "Not allowed."}
        if "$ref" not in node:
            return node
        reference = node["$ref"]
        if not reference.startswith("#/") or reference in seen:
            raise ValueError(
                f"Unsupported external or cyclic alias reference: {reference}"
            )
        target = schema
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return resolve(target, (*seen, reference)) | {
            key: value for key, value in node.items() if key != "$ref"
        }

    def types(node: dict, seen: tuple = ()) -> str:
        if node.get("$ref") in seen:
            return "object"
        seen = (*seen, node.get("$ref")) if "$ref" in node else seen
        node = resolve(node)
        alternatives = node.get("oneOf", node.get("anyOf", []))
        if "type" not in node and alternatives:
            return " / ".join(dict.fromkeys(types(item, seen) for item in alternatives))
        values = node.get("type")
        if values is None:
            if "const" in node:
                value = node["const"]
                values = {
                    str: "string",
                    bool: "boolean",
                    int: "integer",
                    float: "number",
                    list: "array",
                    dict: "object",
                    type(None): "null",
                }[type(value)]
            else:
                values = "object" if "properties" in node else "any"
        values = [values] if isinstance(values, str) else values
        result = []
        for value in values:
            if value == "array" and isinstance(node.get("items"), dict):
                value = f"array<{types(node['items'], seen)}>"
            result.append(value)
        return " / ".join(result)

    def children(node: dict, prefix: str, source: str) -> list[str]:
        node = resolve(node)
        rows = []
        properties = dict(node.get("properties", {}))
        required = set(node.get("required", []))
        for keyword in ("allOf", "anyOf", "oneOf"):
            branches = [resolve(branch) for branch in node.get(keyword, [])]
            for branch in branches:
                for name, definition in branch.get("properties", {}).items():
                    properties.setdefault(name, definition)
            groups = [set(branch.get("required", [])) for branch in branches]
            if groups:
                required.update(
                    set.union(*groups)
                    if keyword == "allOf"
                    else set.intersection(*groups)
                )
        for name, definition in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            origin = "nordicintel" if name == "nordicintel" else source
            rows.extend(property_rows(path, definition, name in required, origin))
        # Dynamic keys are useful only when their values have documented structure.
        dynamic = list(node.get("patternProperties", {}).values())
        if isinstance(node.get("additionalProperties"), dict):
            dynamic.append(node["additionalProperties"])
        for index, definition in enumerate(dynamic):
            path = f"{prefix}.{{key}}" if prefix else "{key}"
            if len(dynamic) > 1:
                path += f"[{index + 1}]"
            origin = "nordicintel" if "nordicintel" in properties else source
            rows.extend(nested(definition, path, origin))
        if isinstance(node.get("items"), dict):
            rows.extend(nested(node["items"], prefix + "[]", source))
        return rows

    def nested(definition: dict, path: str, source: str) -> list[str]:
        target = resolve(definition)
        if "$ref" in definition and target.get("properties"):
            return property_rows(path, definition, False, source)
        return children(target, path, source)

    def property_rows(
        path: str, definition: dict, required: bool, source: str
    ) -> list[str]:
        target = resolve(definition)
        description = target.get("description", "")
        if "const" in target:
            description += (
                " Fixed value: " + json.dumps(target["const"], ensure_ascii=False) + "."
            )
        elif "enum" in target:
            description += (
                " Allowed: " + json.dumps(target["enum"], ensure_ascii=False) + "."
            )
        if target.get("additionalProperties") is True:
            description += " Additional properties allowed."
        conditions = target.get("anyOf", target.get("oneOf", []))
        required_groups = [
            item["required"] for item in conditions if item.get("required")
        ]
        if required_groups:
            description += (
                " Conditional required groups: " + json.dumps(required_groups) + "."
            )
        examples = target.get("examples", [])
        columns = [
            f"`{cell(path)}`",
            source,
            str(required).lower(),
            cell(types(definition)),
            cell(description.strip()),
            "<code>" + cell(json.dumps(examples, ensure_ascii=False)) + "</code>",
        ]
        reference = definition.get("$ref")
        shared = reference and target.get("properties")
        if shared:
            key = (reference, source)
            if key not in references:
                references[key] = len(sections)
                sections.append((reference.rsplit("/", 1)[-1], target, source))
            columns[4] += f" See [properties](#section-{references[key]})."
        rows = ["| " + " | ".join(columns) + " |"]
        if not shared:
            rows.extend(children(target, path, source))
        return rows

    output = [
        "<!-- markdownlint-disable-file MD013 MD033 -->",
        "",
        "# Dataset metadata property reference",
        "",
        f"Generated from [`{SCHEMA}`](../{SCHEMA}); do not edit by hand.",
        "",
        "`required` means required within the containing object, not necessarily at the root. "
        "Conditional requirements are noted in descriptions; consult the schema for full constraints.",
        "",
        "`jstat` marks standard JSON-stat2 fields; `nordicintel` marks fields inside "
        "the NordicIntel namespace and custom namespace conventions (including nested objects). Provider/adapter namespace "
        "contents are open and have no fixed property inventory.",
        "",
        "Paths are relative to each section; `{key}` is a dynamic map key and `[]` an array item. "
        "Shared objects are documented once. Primitive map entries, unused definitions and "
        "arbitrary additional properties are omitted. Examples come only from schema annotations.",
        "",
    ]
    index = 0
    while index < len(sections):
        title, node, source = sections[index]
        output.extend(
            [
                f'<a id="section-{index}"></a>',
                "",
                f"## {cell(title)}",
                "",
                "| Property | Source | Required | Type | Description | Examples |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        output.extend(children(node, "", source))
        output.append("")
        index += 1
    return "\n".join(output)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "src/metadata_harvest/schemas/dataset-metadata.schema.json").read_text(
            encoding="utf-8"
        )
    )
    target = root / "docs/DATASET-METADATA-REFERENCE.md"
    content = render(schema)
    if "--check" in sys.argv:
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            raise SystemExit(
                "Schema reference is stale: run scripts/generate_schema_reference.py"
            )
    else:
        target.write_text(content, encoding="utf-8", newline="\n")

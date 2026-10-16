"""Merge per-language worker documents into one multilingual record.

A seed for the schema work: the rules below are the ones the old assembly used.
Text that differs by language becomes a language map; lists are unioned in a
stable order; a non-text value that differs between languages is kept from the
first document and the alternatives are retained under ``conflicts``.
"""

from __future__ import annotations

import json
from copy import deepcopy

TEXT = ("label", "note", "source")
"""Document fields whose values are language-specific text."""


def _key(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _union(left: list, right: list) -> list:
    values = {_key(v): v for v in [*left, *right]}
    return [values[key] for key in sorted(values)]


def _merge_text(target: dict, language: str, value) -> None:
    """Add one language's text to a language map, keeping earlier entries."""
    if value is None:
        return
    if isinstance(value, list):
        target[language] = list(dict.fromkeys(target.get(language, []) + value))
    else:
        target.setdefault(language, value)


def _retain_conflict(attributes: dict, key: str, left, right) -> None:
    # Snapshot before changing the container: left may be the container itself.
    values = deepcopy(attributes.get("conflicts", {}).get(key, []) + [left, right])
    unique = {}
    for value in values:
        unique.setdefault(_key(value), value)
    attributes.setdefault("conflicts", {})[key] = list(unique.values())


def _merge_category(target: dict, language: str, source: dict) -> None:
    for field in ("label", "note", "unit"):
        if field in source:
            target.setdefault(field, {}).setdefault(language, deepcopy(source[field]))
    for field, value in source.items():
        if field in ("label", "note", "unit"):
            continue
        if field not in target:
            target[field] = deepcopy(value)
        elif _key(target[field]) != _key(value):
            _retain_conflict(target, field, target[field], value)


def merge(documents: list[dict]) -> dict:
    """One multilingual record from the per-language documents of one dataset."""
    if not documents:
        raise ValueError("Nothing to merge")
    identities = {
        (
            d["extension"]["nordicintel"]["provider_code"],
            d["extension"]["nordicintel"]["dataset_code"],
        )
        for d in documents
    }
    if len(identities) != 1:
        raise ValueError("Cannot merge different datasets")
    ordered = sorted(documents, key=lambda d: d["extension"]["nordicintel"]["language"])
    result: dict = {"dimension": {}, "extension": {}}
    for document in ordered:
        language = document["extension"]["nordicintel"]["language"]
        for field, value in document.items():
            if field in TEXT:
                _merge_text(result.setdefault(field, {}), language, value)
            elif field == "dimension":
                for code, dimension in value.items():
                    target = result["dimension"].setdefault(code, {"category": {}})
                    for name, item in dimension.items():
                        if name in ("label", "note"):
                            _merge_text(target.setdefault(name, {}), language, item)
                        elif name == "category":
                            _merge_category(target["category"], language, item)
                        elif name not in target:
                            target[name] = deepcopy(item)
                        elif _key(target[name]) != _key(item):
                            _retain_conflict(target, name, target[name], item)
            elif field == "extension":
                for namespace, values in value.items():
                    target = result["extension"].setdefault(namespace, {})
                    for name, item in values.items():
                        if name == "language":
                            target.setdefault("languages", [])
                            target["languages"] = _union(target["languages"], [item])
                        elif name not in target:
                            target[name] = deepcopy(item)
                        elif isinstance(item, list) and isinstance(target[name], list):
                            target[name] = _union(target[name], item)
                        elif _key(target[name]) != _key(item):
                            _retain_conflict(
                                result["extension"]["nordicintel"],
                                f"{namespace}/{name}",
                                target[name],
                                item,
                            )
            elif field not in result:
                result[field] = deepcopy(value)
            elif isinstance(value, list) and field in ("link",):
                result[field] = _union(result[field], value)
            elif _key(result[field]) != _key(value):
                _retain_conflict(
                    result["extension"]["nordicintel"], field, result[field], value
                )
    return result

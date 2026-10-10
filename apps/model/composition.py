"""Pure composition of multilingual datasets and reference codelists."""

from __future__ import annotations

import json
from copy import deepcopy

from apps.model.models import (
    CodeList,
    Dataset,
    HarvestContribution,
    Reference,
)


def _union(left, right):
    values = {
        json.dumps(
            v.model_dump(mode="json") if hasattr(v, "model_dump") else v,
            sort_keys=True,
            ensure_ascii=False,
        ): v
        for v in [*left, *right]
    }
    return [values[key] for key in sorted(values)]


def _merge_text(target, source):
    for lang, texts in source.items():
        target[lang] = list(dict.fromkeys(target.get(lang, []) + texts))


def assemble(
    contributions: list[HarvestContribution], *, include_codelists=True
) -> tuple[list[Dataset], list[CodeList]]:
    """Combine languages and scopes; retain each independently replaceable contribution."""
    groups, schemes = {}, {}
    for contribution in sorted(
        contributions, key=lambda c: (c.dataset.id, c.dataset.distributions[0].id)
    ):
        current = groups.setdefault(contribution.dataset.id, [])
        current.append(contribution.dataset)
        for scheme in contribution.codelists if include_codelists else ():
            if scheme.id not in schemes:
                schemes[scheme.id] = scheme.model_copy(deep=True)
                continue
            schemes[scheme.id] = merge_codelists(schemes[scheme.id], scheme)
    datasets = []
    for _identity, parts in sorted(groups.items()):
        base = parts[0].model_copy(deep=True)
        for part in parts[1:]:
            for key, value in part.source_evidence.items():
                if key not in base.source_evidence:
                    base.source_evidence[key] = deepcopy(value)
                elif isinstance(value, list) and isinstance(
                    base.source_evidence[key], list
                ):
                    base.source_evidence[key] = _union(base.source_evidence[key], value)
                elif base.source_evidence[key] != value:
                    base.source_evidence.setdefault(
                        "conflicting_assertions", []
                    ).append({"property": key, "value": deepcopy(value)})
            for key in ("title", "description", "notes", "keywords"):
                _merge_text(getattr(base, key), getattr(part, key))
            for key in (
                "catalogs",
                "identifiers",
                "temporal",
                "temporal_resolution",
                "landing_page",
                "documentation",
                "contacts",
                "themes",
                "dimensions",
                "distributions",
            ):
                setattr(base, key, _union(getattr(base, key), getattr(part, key)))
            if part.modified and (
                base.modified is None or part.modified > base.modified
            ):
                base.modified = part.modified
        for key in ("official_statistics", "discontinued"):
            values = {getattr(part, key) for part in parts}
            setattr(base, key, values.pop() if len(values) == 1 else None)
        # Distribution-level structures remain authoritative, even across translations.
        base.structure = (
            Reference(id=base.distributions[0].structure.id)
            if len(base.distributions) == 1
            and base.distributions[0].structure is not None
            else None
        )
        datasets.append(base)
    return datasets, [schemes[k] for k in sorted(schemes)]


def merge_codelists(left: CodeList, right: CodeList) -> CodeList:
    """Join language labels and retain genuinely conflicting non-language assertions."""
    if left.id != right.id:
        raise ValueError("Cannot merge different codelist identities")
    result = left.model_copy(deep=True)
    _merge_text(result.label, right.label)
    codes = {c.code: c for c in result.codes}
    for incoming in right.codes:
        if incoming.code not in codes:
            result.codes.append(incoming.model_copy(deep=True))
            continue
        code = codes[incoming.code]
        _merge_text(code.label, incoming.label)
        _merge_text(code.notes, incoming.notes)
        for field in (
            "unit",
            "coordinates",
            "aggregation_map",
            "broader",
            "additional_attributes",
        ):
            a, b = getattr(code, field), getattr(incoming, field)
            if (
                json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)
                and b is not None
            ):
                _retain_conflict(code.additional_attributes, field, a, b)
    for namespace, values in right.additional_attributes.items():
        target = result.additional_attributes.setdefault(namespace, {})
        for key, value in values.items():
            if key not in target:
                target[key] = deepcopy(value)
            elif json.dumps(target[key], sort_keys=True) != json.dumps(
                value, sort_keys=True
            ):
                _retain_conflict(
                    result.additional_attributes,
                    namespace + "/" + key,
                    target[key],
                    value,
                )
    for language, order in right.category_order.items():
        if language not in result.category_order:
            result.category_order[language] = list(order)
        elif result.category_order[language] != order:
            _retain_conflict(
                result.additional_attributes,
                "category_order/" + language,
                result.category_order[language],
                order,
            )
    languages = sorted(
        set(
            left.source_identity.get("languages", [])
            + right.source_identity.get("languages", [])
        )
    )
    if languages:
        result.source_identity["languages"] = languages
    return result


def _retain_conflict(attributes, key, left, right):
    # Snapshot before changing the container: left may be the container itself.
    values = deepcopy(attributes.get("conflicts", {}).get(key, []) + [left, right])
    unique = {}
    for value in values:
        unique.setdefault(json.dumps(value, sort_keys=True), value)
    attributes.setdefault("conflicts", {})[key] = list(unique.values())

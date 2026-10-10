"""Resolve authoritative repository configuration into worker settings."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

from apps.harvest.registry import ADAPTERS
from apps.model.configuration import SourceConfiguration
from apps.paths import CONFIG

_active_configuration = ContextVar("source_configuration", default=None)


def activate_configuration(configuration):
    """Select a validated configuration for this worker's execution context."""
    _active_configuration.set(configuration)


@contextmanager
def use_configuration(configuration=None):
    token = _active_configuration.set(configuration)
    try:
        yield
    finally:
        _active_configuration.reset(token)


def load_configuration(path: str | Path | None = None) -> SourceConfiguration:
    """Validate the repository configuration or an explicit three-file directory."""
    if path is None:
        return _active_configuration.get() or _default_configuration()
    root = Path(path)
    if root.is_file():
        raise ValueError("--config must name the directory containing the three files")
    return _load(root)


def _load(root) -> SourceConfiguration:
    values = {}
    for filename, fields in (
        ("adapters.json", {"adapters"}),
        ("organisations.json", {"organisations"}),
        ("catalogs.json", {"services", "catalogs"}),
    ):
        document = json.loads(root.joinpath(filename).read_text(encoding="utf-8"))
        if document.get("configuration_version") != 1 or set(document) != fields | {
            "configuration_version"
        }:
            raise ValueError(f"Invalid configuration envelope: {filename}")
        values.update({k: document[k] for k in fields})
    configuration = SourceConfiguration.model_validate(values)
    for adapter in configuration.adapters:
        if adapter not in ADAPTERS or not ADAPTERS[adapter].available:
            raise ValueError(f"Unknown adapter: {adapter}")
    return configuration


@cache
def _default_configuration() -> SourceConfiguration:
    return _load(CONFIG)


def providers_for_adapter(
    adapter: str,
    *,
    configuration: SourceConfiguration | None = None,
    legacy_path: str | Path | None = None,
) -> list[dict]:
    """Project validated settings to the existing worker dictionaries.

    ``legacy_path`` is an explicit legacy-list import, never an implicit fallback.
    Values and source ordering are preserved; only metadata ownership uses the
    organisation references. No caller is given a mutable cached configuration.
    """
    if legacy_path is not None:
        rows = json.loads(Path(legacy_path).read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(
            not isinstance(r, dict) or not r.get("code") for r in rows
        ):
            raise ValueError("Legacy providers must be a list of source configurations")
        if len({r["code"] for r in rows}) != len(rows):
            raise ValueError("Duplicate legacy source alias")
        return rows
    configuration = configuration or load_configuration()
    if adapter not in configuration.adapters:
        raise ValueError(f"Unknown adapter: {adapter}")
    definition = configuration.adapters[adapter]
    rows = []
    for binding in sorted(configuration.services.values(), key=lambda s: s.order):
        if binding.adapter != adapter:
            continue
        row = {
            "code": binding.source_code,
            definition.endpoint_parameter: binding.endpoint_url[0],
        }
        row.update(definition.defaults | binding.options)
        catalogs = [
            c for c in configuration.catalogs.values() if binding.id in c.services
        ]
        if definition.uses_catalog_label:
            row["label"] = (
                next(iter(catalogs[0].title.values()))[0]
                if len(catalogs) == 1
                else urlsplit(binding.endpoint_url[0]).netloc
            )
        if len(catalogs) == 1 and catalogs[0].description:
            row["description"] = {
                lang: texts[0] for lang, texts in catalogs[0].description.items()
            }
        rows.append(json.loads(json.dumps(row, ensure_ascii=False)))
    return rows


def legacy_configuration(
    configuration: SourceConfiguration, adapter: str, rows: list[dict]
) -> SourceConfiguration:
    """Apply explicit legacy-list overrides to service metadata as well as workers."""
    document = configuration.model_dump()
    endpoint_key = configuration.adapters[adapter].endpoint_parameter
    existing = {
        b.source_code: b
        for b in configuration.services.values()
        if b.adapter == adapter
    }
    next_order = max((b.order for b in existing.values()), default=-1) + 1
    for row in rows:
        code = row["code"]
        previous = existing.get(code)
        identity = (
            previous.id
            if previous
            else f"urn:nordicintel:service:custom:{adapter}:{code}"
        )
        binding = (
            previous.model_dump()
            if previous
            else {
                "id": identity,
                "adapter": adapter,
                "source_code": code,
                "identity_namespace": code,
                "order": next_order,
            }
        )
        if not previous:
            next_order += 1
        binding["endpoint_url"] = [row[endpoint_key]]
        binding["options"] = {
            k: v for k, v in row.items() if k not in {"code", endpoint_key}
        }
        document["services"][identity] = binding
    return SourceConfiguration.model_validate(document)

"""Keep authoritative contracts, documented links and provider registration aligned."""

import importlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def test_generated_schema_reference_is_current():
    subprocess.run(
        [sys.executable, "scripts/generate_schema_reference.py", "--check"],
        cwd=ROOT,
        check=True,
    )


def test_local_documentation_links_resolve():
    documents = [
        ROOT / "README.md",
        *(ROOT / "docs").rglob("*.md"),
        ROOT / "src/metadata_harvest/schemas/README.md",
    ]
    broken = []
    for path in documents:
        for target in re.findall(
            r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")
        ):
            target = target.split("#", 1)[0].strip("<>")
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            if not (path.parent / unquote(target)).resolve().exists():
                broken.append(f"{path.relative_to(ROOT)}: {target}")
    assert not broken, "\n".join(broken)


def test_provider_entry_points_and_placeholders_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    entries = project["entry-points"]["nordicintel.retrieval"]
    expected = {}
    for adapter, config in [
        ("pxweb_v1", "providers.json"),
        ("pxweb_v2", "providers.pxweb_v2.json"),
        ("kolada", "providers.kolada.json"),
        ("uka", "providers.uka.json"),
        ("sdb", "providers.sdb.json"),
        ("trafa", "providers.trafa.json"),
    ]:
        for provider in json.loads((ROOT / config).read_text(encoding="utf-8")):
            expected[provider["code"]] = (
                f"metadata_harvest.adapters.{adapter}.retrieval:retrieve"
            )
    assert entries == expected
    assert project["dependencies"] == ["aiohttp>=3.9,<4"]
    from metadata_harvest.registry import ADAPTERS

    for name, spec in ADAPTERS.items():
        if not spec.available:
            assert (ROOT / spec.preparation).is_file()
            module = importlib.import_module(f"metadata_harvest.adapters.{name}")
            assert module.AVAILABLE is False
            import pytest

            for operation in (
                module.discover_provider,
                module.fetch_metadata,
                module.retrieve,
            ):
                with pytest.raises(NotImplementedError):
                    operation()

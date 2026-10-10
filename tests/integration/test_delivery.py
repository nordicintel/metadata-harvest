"""Keep authoritative contracts, documented links and provider registration aligned."""

import re
import subprocess
import sys
import tomllib
from urllib.parse import unquote

from apps.paths import ROOT


def test_generated_schema_reference_is_current():
    subprocess.run(
        [sys.executable, "scripts/generate/generate_schema_reference.py", "--check"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [sys.executable, "scripts/generate/generate_configuration.py", "--check"],
        cwd=ROOT,
        check=True,
    )


def test_local_documentation_links_resolve():
    # Tracked documents only: ignored research material under docs/ may link
    # to files that exist elsewhere.
    documents = [
        ROOT / "README.md",
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("schemas/*.md"),
    ]
    documents = [
        p
        for p in documents
        if subprocess.run(["git", "check-ignore", "-q", str(p)], cwd=ROOT).returncode
        != 0
    ]
    assert ROOT / "README.md" in documents
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


def test_provider_entry_points_and_runtime_dependencies_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    entries = project["entry-points"]["nordicintel.retrieval"]
    expected = {}
    from apps.configuration import load_configuration, providers_for_adapter

    for adapter in load_configuration().adapters:
        for provider in providers_for_adapter(adapter):
            expected[provider["code"]] = f"retrieval.data_adapters.{adapter}:retrieve"
    assert entries == expected
    assert project["dependencies"] == [
        "aiohttp>=3.9,<4",
    ]

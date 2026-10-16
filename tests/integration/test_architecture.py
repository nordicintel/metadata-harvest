"""Executable ownership rules for repository applications and the exported library."""

import ast
import subprocess
import sys

from apps.configuration import load_configuration
from apps.paths import CONFIG, ROOT, SCHEMAS


def imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module


def test_retrieval_has_only_its_runtime_dependencies():
    for path in (ROOT / "retrieval").rglob("*.py"):
        for name in imports(path):
            assert name.split(".")[0] in sys.stdlib_module_names | {
                "retrieval",
                "aiohttp",
            }, (path, name)


def test_model_does_not_import_application_behaviour():
    for path in (ROOT / "apps/model").glob("*.py"):
        for name in imports(path):
            if name.startswith("apps."):
                assert name.startswith("apps.model.") or name == "apps.paths", (
                    path,
                    name,
                )


def test_retired_implementation_paths_have_no_source_or_callers():
    assert not list((ROOT / "harvesting").rglob("*.py"))
    assert not list((ROOT / "retrieval/adapters").glob("*.py"))
    for directory in ("apps", "retrieval", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            assert not any(
                name == "harvesting"
                or name.startswith(("harvesting.", "retrieval.adapters"))
                for name in imports(path)
            ), path


def test_configuration_and_profile_have_one_authoritative_location(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    configuration = load_configuration()
    assert len(configuration.adapters) == 12
    assert SCHEMAS.joinpath("dataset-metadata.schema.json").is_file()
    assert not list(ROOT.glob("*.json"))
    assert [p.name for p in ROOT.glob("*.toml")] == ["pyproject.toml"]
    for filename in ("adapters.json", "organisations.json", "catalogs.json"):
        assert CONFIG.joinpath(filename).is_file()
        assert not list((ROOT / "apps").rglob(filename))


def module_imports(path):
    """Module-level imports only: typed-boundary wrappers may import lazily."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.module


def test_harvest_does_not_import_parsing():
    for path in (ROOT / "apps/harvest").rglob("*.py"):
        for name in module_imports(path):
            assert not name.startswith("apps.parsing"), (path, name)


def test_parsing_does_not_import_catalog_or_harvest_workflows():
    for path in (ROOT / "apps/parsing").rglob("*.py"):
        for name in imports(path):
            assert not name.startswith(
                ("apps.catalog", "apps.publication.candidates")
            ), (
                path,
                name,
            )


def test_statdcat_conversion_lives_in_parsing():
    assert not (ROOT / "apps/harvest/contributions.py").exists()
    assert not (ROOT / "apps/harvest/accepted.py").exists()
    assert (ROOT / "apps/parsing/contributions.py").is_file()
    assert (ROOT / "apps/parsing/accepted.py").is_file()


def test_every_repository_command_loads_without_running_work():
    for name in (
        "harvest",
        "parsing",
        "catalog",
        "portal",
        "scheduling",
        "monitor",
        "outcome",
    ):
        subprocess.run(
            [sys.executable, "-m", f"apps.operations.{name}", "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )

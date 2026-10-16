"""Executable ownership rules for repository applications and the exported library."""

import ast
import subprocess
import sys

import registry
from apps.paths import CONFIG, ROOT


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
    registry.load()
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


PUBLIC = {"__init__", "client", "harvest", "parse", "mapping"}


def test_adapter_folders_keep_the_shape_and_private_modules_stay_inside():
    from apps.harvest.metadata_adapters import ADAPTERS

    folders = ROOT / "apps/harvest/metadata_adapters"
    private = {}
    for name in ADAPTERS:
        if name == "pxweb_v1":  # keeps its multi-job pipeline shape
            continue
        modules = {p.stem for p in (folders / name).glob("*.py")}
        assert {"__init__", "client", "harvest", "parse"} <= modules, name
        assert (folders / name / "__init__.py").read_text(encoding="utf-8") == ""
        private[name] = modules - PUBLIC
    for directory in ("apps", "retrieval", "scripts", "registry", "schema", "harvest"):
        for path in (ROOT / directory).rglob("*.py"):
            for imported in imports(path):
                for name, modules in private.items():
                    prefix = f"apps.harvest.metadata_adapters.{name}."
                    if (
                        imported.startswith(prefix)
                        and imported[len(prefix) :] in modules
                    ):
                        inside = folders / name in path.parents
                        assert inside or directory == "scripts", (path, imported)


def test_every_repository_command_loads_without_running_work():
    for name in (
        "harvest",
        "parsing",
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

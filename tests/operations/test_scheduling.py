"""Single-source schedules and generated workflow blocks."""

import pytest

from apps.operations.scheduling import load, resolve, sync
from apps.paths import ROOT


def test_modes_and_manual_override():
    config = load(ROOT / "config/schedules.toml")
    assert resolve(config, "pxweb_v2", event="schedule", cron="15 6 1,15 * *")["full"]
    assert not resolve(
        config, "pxweb_v2", event="schedule", cron="15 6 2-14,16-31 * *"
    )["full"]
    assert resolve(config, "pxweb_v2", event="workflow_dispatch", full=True)["enabled"]
    assert resolve(config, "kolada", event="workflow_dispatch")["enabled"]
    assert not resolve(config, "uka", event="schedule", cron="15 5 * * 1")["full"]
    assert resolve(config, "pxweb_v1", event="schedule", cron="15 4 * * 0")["full"]
    assert resolve(config, "uka", event="workflow_dispatch", full=True)["full"]
    with pytest.raises(ValueError):
        resolve(config, "kolada", event="workflow_dispatch", full=True)
    with pytest.raises(ValueError):
        resolve(config, "pxweb_v1", event="schedule", cron="not a configured schedule")


def test_config_rejects_invalid_modes_and_schedules(tmp_path):
    content = (ROOT / "config/schedules.toml").read_text()
    for edited in (
        content.replace('mode = "incremental"', 'mode = "unknown"', 1),
        content.replace("15 4 * * 1-6", "80 4 * * 1-6"),
        content.replace("[adapters.kolada]", "[adapters.unavailable]"),
    ):
        target = tmp_path / "schedules.toml"
        target.write_text(edited)
        with pytest.raises(ValueError):
            load(target)


def test_generated_workflows_match_config():
    assert sync(ROOT, check=True) == []


def test_sync_changes_only_marked_blocks(tmp_path):
    import shutil

    (tmp_path / "config").mkdir()
    shutil.copyfile(ROOT / "config/schedules.toml", tmp_path / "config/schedules.toml")
    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    config = tmp_path / "config/schedules.toml"
    config.write_text(config.read_text().replace("15 4 * * 1-6", "20 4 * * 1-6"))
    assert sync(tmp_path, check=True) == ["harvest.yml"]
    sync(tmp_path)
    assert sync(tmp_path, check=True) == []
    assert "20 4 * * 1-6" in (tmp_path / ".github/workflows/harvest.yml").read_text()


def test_paused_settings_disable_every_scheduled_workflow(tmp_path):
    import shutil

    (tmp_path / "config").mkdir()
    shutil.copyfile(ROOT / "config/schedules.toml", tmp_path / "config/schedules.toml")
    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    settings = tmp_path / "config/settings.toml"
    settings.write_text("[scheduling]\npaused = true\n", encoding="utf-8")
    assert sync(tmp_path)
    for path in (tmp_path / ".github/workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        if "# BEGIN GENERATED ENABLEMENT" in text:
            assert "github.event_name == 'workflow_dispatch' || false" in text
            assert "github.event_name == 'workflow_dispatch' || true" not in text
    settings.write_text("[scheduling]\npaused = false\n", encoding="utf-8")
    sync(tmp_path)
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        restored = tmp_path / ".github/workflows" / path.name
        assert restored.read_bytes() == path.read_bytes()


def test_settings_reject_unknown_or_non_boolean_values(tmp_path):
    from apps.settings import load as load_settings

    assert load_settings(tmp_path)["scheduling"]["paused"] is False
    for content in (
        "[scheduling]\npaused = 1\n",
        "[scheduling]\npause = true\n",
        "[unknown]\nvalue = true\n",
    ):
        (tmp_path / "settings.toml").write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_settings(tmp_path)


def test_workflows_have_unique_keys_and_valid_step_shapes():
    import yaml

    class StrictLoader(yaml.BaseLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key, value in node.value:
            name = loader.construct_object(key)
            assert name not in result, f"Duplicate workflow key: {name}"
            result[name] = loader.construct_object(value)
        return result

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping
    )
    workflows = {}
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=StrictLoader)
        workflows[path.name] = workflow
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                assert ("run" in step) != ("uses" in step), (path.name, step)
                assert "with" not in step or "uses" in step, (path.name, step)
    v1 = workflows["harvest.yml"]["jobs"]
    assert any(s.get("id") == "plan" for s in v1["prepare"]["steps"])
    assert any("playwright install" in s.get("run", "") for s in v1["refresh"]["steps"])
    assert any(
        s.get("uses", "").startswith("actions/download-artifact@")
        for s in v1["collect-metadata"]["steps"]
    )


def test_harvest_workflows_never_dispatch_the_catalog():
    import yaml

    for path in sorted((ROOT / ".github/workflows").glob("harvest*.yml")):
        content = path.read_text(encoding="utf-8")
        assert "catalog.yml" not in content, path.name
        workflow = yaml.load(content, Loader=yaml.BaseLoader)
        steps = [
            step for job in workflow["jobs"].values() for step in job.get("steps", [])
        ]
        assert not any(step.get("id") == "catalog" for step in steps), path.name
        # Only the PXWeb v1 job continues across waves; the others finish in one run.
        assert any(
            step.get("name") == "Continue unfinished work" for step in steps
        ) == (path.name == "harvest.yml")

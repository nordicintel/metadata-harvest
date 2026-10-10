import subprocess
import sys
from pathlib import Path

import pytest

import apps.operations.harvest as runner
from apps.configuration import providers_for_adapter


def test_v2_runner_selects_own_config_data_and_full_mode(monkeypatch, capsys):
    from apps.harvest.metadata_adapters.pxweb_v2 import workflow

    received = {}

    async def run(provider, data, output, **options):
        received.update(provider=provider, data=data, output=output, **options)
        return {
            "run_id": "r",
            "wave": 1,
            "status": "success",
            "continue": False,
            "counts": {},
        }

    monkeypatch.setattr(workflow, "run", run)
    monkeypatch.setattr(
        sys, "argv", ["runner", "run", "--adapter", "pxweb_v2", "--full"]
    )
    runner.main()
    assert received["provider"] == providers_for_adapter("pxweb_v2")[0]
    assert received["data"] == Path("data/pxweb_v2")
    assert received["output"] == Path("output/pxweb_v2")
    assert received["full"] is True
    assert "status=success" in capsys.readouterr().out


def test_scoped_publish_leaves_other_adapter_unstaged(tmp_path, monkeypatch):
    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=check, capture_output=True, text=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "README").write_text("initial")
    git("add", "README")
    git("commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    for adapter in ("pxweb_v1", "pxweb_v2"):
        folder = tmp_path / "data" / adapter
        folder.mkdir(parents=True)
        (folder / "result.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    runner.publish("v2", data=Path("data/pxweb_v2"))
    assert (
        git("show", "--format=", "--name-only", "HEAD").stdout.strip()
        == "data/pxweb_v2/result.json"
    )
    assert git("status", "--porcelain").stdout.find("data/pxweb_v1/") >= 0
    git("add", "data/pxweb_v1")
    with pytest.raises(RuntimeError, match="unrelated staged"):
        runner.publish("must reject", data=Path("data/pxweb_v2"))

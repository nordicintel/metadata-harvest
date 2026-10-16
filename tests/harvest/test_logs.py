import io
import logging

import pytest

from apps.harvest import logs
from apps.settings import load as load_settings


@pytest.fixture(autouse=True)
def restore_default_logging():
    yield
    logs.configure("INFO")


def _emit(stream, level):
    logs.configure(level, stream=stream)
    logging.getLogger("sdb_logger").info("hello")
    logging.getLogger("retrieval.data_adapters.demo").debug("fine")
    logging.getLogger("apps.parsing.build").warning("careful")
    return stream.getvalue()


def test_adapter_loggers_inherit_level_and_readable_format():
    output = _emit(io.StringIO(), "INFO")
    lines = output.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("INFO sdb_logger: hello")
    assert lines[1].endswith("WARNING apps.parsing.build: careful")
    assert _emit(io.StringIO(), "DEBUG").count("\n") == 3
    assert _emit(io.StringIO(), "WARNING").count("\n") == 1


def test_off_silences_adapters_and_reconfigure_does_not_duplicate_handlers():
    assert _emit(io.StringIO(), "OFF") == ""
    assert _emit(io.StringIO(), "INFO").count("\n") == 2
    for name in logs.NAMESPACES:
        handlers = logging.getLogger(name).handlers
        assert [h.get_name() for h in handlers].count("metadata-harvest") == 1


def test_level_comes_from_settings(tmp_path, monkeypatch):
    assert load_settings(tmp_path)["logging"]["level"] == "INFO"
    settings = tmp_path / "settings.toml"
    settings.write_text('[logging]\nlevel = "OFF"\n', encoding="utf-8")
    assert load_settings(tmp_path)["logging"]["level"] == "OFF"
    for content in ('[logging]\nlevel = "VERBOSE"\n', "[logging]\nlevel = true\n"):
        settings.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_settings(tmp_path)
    monkeypatch.setattr(logs, "load_settings", lambda: {"logging": {"level": "OFF"}})
    stream = io.StringIO()
    logs.configure(stream=stream)
    logging.getLogger("apps.harvest.metadata_adapters.demo").error("silent")
    assert stream.getvalue() == ""


def test_every_adapter_module_uses_its_deterministic_logger():
    import re
    from pathlib import Path

    from apps.harvest.metadata_adapters import ADAPTERS

    for adapter in ADAPTERS:
        assert logs.adapter_logger(adapter).name == f"{adapter}_logger"
        files = list(Path("apps/harvest/metadata_adapters", adapter).glob("*.py"))
        retrieval = Path("retrieval/data_adapters", adapter + ".py")
        if retrieval.exists():
            files.append(retrieval)
        for file in files:
            for found in re.findall(r"getLogger\(([^)]*)\)", file.read_text("utf-8")):
                assert found == f'"{adapter}_logger"', f"{file}: {found}"

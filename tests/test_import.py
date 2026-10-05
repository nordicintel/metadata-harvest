from importlib import import_module


def test_package_imports():
    assert import_module("metadata_harvest") is not None

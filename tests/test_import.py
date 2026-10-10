from importlib import import_module, util


def test_package_imports():
    assert import_module("harvesting") is not None


def test_pxweb_functions_belong_to_the_adapter():
    assert callable(
        import_module("harvesting.adapters.pxweb_v1.discovery").get_pxweb_tables
    )
    assert callable(
        import_module("harvesting.adapters.pxweb_v1.metadata").get_pxweb_metadata
    )
    assert util.find_spec("harvesting.pxweb") is None
    assert util.find_spec("harvesting.metadata") is None
    assert not hasattr(import_module("harvesting"), "get_pxweb_tables")
    assert not hasattr(import_module("harvesting"), "get_pxweb_metadata")

from importlib import import_module


def test_package_imports():
    assert import_module("harvesting") is not None


def test_pxweb_v1_public_imports_remain_compatible():
    package = import_module("harvesting")
    discovery = import_module("harvesting.adapters.pxweb_v1.discovery")
    metadata = import_module("harvesting.adapters.pxweb_v1.metadata")
    assert package.get_pxweb_tables is discovery.get_pxweb_tables
    assert package.get_pxweb_metadata is metadata.get_pxweb_metadata
    assert (
        import_module("harvesting.pxweb").get_pxweb_tables is discovery.get_pxweb_tables
    )
    assert (
        import_module("harvesting.metadata").get_pxweb_metadata
        is metadata.get_pxweb_metadata
    )

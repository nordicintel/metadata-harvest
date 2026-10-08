from importlib import import_module


def test_package_imports():
    assert import_module("metadata_harvest") is not None


def test_pxweb_v1_public_imports_remain_compatible():
    package = import_module("metadata_harvest")
    discovery = import_module("metadata_harvest.adapters.pxweb_v1.discovery")
    metadata = import_module("metadata_harvest.adapters.pxweb_v1.metadata")
    assert package.get_pxweb_tables is discovery.get_pxweb_tables
    assert package.get_pxweb_metadata is metadata.get_pxweb_metadata
    assert (
        import_module("metadata_harvest.pxweb").get_pxweb_tables
        is discovery.get_pxweb_tables
    )
    assert (
        import_module("metadata_harvest.metadata").get_pxweb_metadata
        is metadata.get_pxweb_metadata
    )

from importlib import import_module


def test_application_and_retrieval_imports():
    for module in ("apps.model.models", "apps.configuration", "retrieval"):
        assert import_module(module) is not None


def test_pxweb_functions_belong_to_the_adapter():
    assert callable(
        import_module(
            "apps.harvest.metadata_adapters.pxweb_v1.discovery"
        ).get_pxweb_tables
    )
    assert callable(
        import_module(
            "apps.harvest.metadata_adapters.pxweb_v1.metadata"
        ).get_pxweb_metadata
    )

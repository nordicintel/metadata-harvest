"""The three source files resolve to safe, compatible harvesting configuration."""

import json
from pathlib import Path

import pytest

from apps.configuration import load_configuration, providers_for_adapter
from apps.model.models import DataService


def test_all_existing_worker_settings_survive_configuration_conversion():
    baseline = json.loads(
        Path("tests/fixtures/configuration-legacy.json").read_text(encoding="utf-8")
    )
    configuration = load_configuration()
    assert len(configuration.adapters) == 12
    for adapter, previous in baseline.items():
        actual = providers_for_adapter(adapter, configuration=configuration)
        # Replace the former hostname display label with the known catalog title.
        for item in previous:
            if item["code"] == "vgregionentryscapenet":
                item["label"] = "Västra Götalandsregionens öppna data"
        assert actual == previous


def test_explicit_false_zero_and_empty_values_override_adapter_defaults():
    configuration = load_configuration().model_copy(deep=True)
    definition = configuration.adapters["entryscape"]
    definition.defaults.update(limit=4, enabled=True, filters=["default"])
    binding = next(
        s for s in configuration.services.values() if s.adapter == "entryscape"
    )
    binding.options.update(limit=0, enabled=False, filters=[])
    actual = providers_for_adapter("entryscape", configuration=configuration)
    row = next(row for row in actual if row["code"] == binding.source_code)
    assert (row["limit"], row["enabled"], row["filters"]) == (0, False, [])


def test_cross_file_references_and_alias_collisions_are_rejected():
    configuration = load_configuration()
    broken = configuration.model_dump()
    binding = next(iter(broken["services"].values()))
    binding["adapter"] = "missing"
    with pytest.raises(ValueError, match="adapter"):
        type(configuration).model_validate(broken)
    broken = configuration.model_dump()
    catalog = next(iter(broken["catalogs"].values()))
    catalog["publisher"] = "missing"
    with pytest.raises(ValueError, match="organisation"):
        type(configuration).model_validate(broken)


def test_concrete_services_accept_endpoints_and_publishers():
    service = DataService(
        id="urn:nordicintel:service:scb:pxweb_v2:statistics",
        adapter="pxweb_v2",
        endpoint_url=["https://statistikdatabasen.scb.se/api/v2"],
        publisher=[{"id": "urn:nordicintel:agent:scb", "name": {"sv": ["SCB"]}}],
    )
    assert DataService.from_jsonld(service.to_jsonld()) == service


def test_shared_host_keeps_catalog_publishers_and_discovery_scope_separate():
    from apps.harvest.metadata_adapters.entryscape.discovery import within_catalog_scope
    from apps.harvest.sources import resolve_source
    from apps.model.models import SourceIdentity

    source = SourceIdentity(
        adapter="entryscape",
        source_code="freeentryscapecom",
        dataset_code="a7c572c8-2146-4285-8947-7f2f378ee120",
        language="sv",
    )
    catalogs, service = resolve_source(
        source, "https://free.entryscape.com/rowstore/dataset/x", {}
    )
    assert [c.publisher[0].id for c in catalogs] == ["urn:nordicintel:agent:digidel"]
    assert service.publisher == []  # Publishing data does not imply running the host.
    assert service.endpoint_url == ["https://free.entryscape.com"]
    assert within_catalog_scope(
        {"code": source.source_code}, {"dataset_code": source.dataset_code}
    )
    assert not within_catalog_scope(
        {"code": source.source_code},
        {
            "dataset_code": "other",
            "uri": "https://free.entryscape.com/store/618/resource/1",
        },
    )
    assert within_catalog_scope(
        {"code": source.source_code},
        {
            "dataset_code": "new",
            "uri": "https://free.entryscape.com/store/640/resource/1",
        },
    )


def test_unattributed_refresh_preserves_findings_across_languages():
    from apps.model.composition import assemble
    from apps.parsing.contributions import convert_dataset
    from tests.support.worker_document import legacy

    contributions = []
    for language in ("sv", "en"):
        native = legacy(language, code="unattributed", provider="freeentryscapecom")
        native["extension"]["entryscape"] = native["extension"].pop("pxweb_v2")
        contributions.append(convert_dataset(native, adapter="entryscape"))
    datasets, _ = assemble(contributions)
    assert len(datasets) == 1
    assert not datasets[0].catalogs
    assert len(datasets[0].source_evidence["attribution_findings"]) == 2


def test_homepage_is_an_iri_and_previous_codelists_remain_readable():
    from rdflib import Graph, URIRef

    from apps.model.models import Agent, CodeList
    from apps.model.vocabulary import PROFILE1_CONTEXT

    agent = Agent(id="urn:test:org", homepage=["https://example.org/"])
    graph = Graph().parse(data=json.dumps(agent.to_jsonld()), format="json-ld")
    assert (
        URIRef(agent.id),
        URIRef("http://xmlns.com/foaf/0.1/homepage"),
        URIRef("https://example.org/"),
    ) in graph
    previous = CodeList(id="urn:test:codes").to_jsonld()
    previous["@context"] = PROFILE1_CONTEXT
    assert CodeList.from_jsonld(previous).id == "urn:test:codes"


def test_explicit_legacy_overrides_feed_service_metadata_and_context_resets():
    from apps.configuration import legacy_configuration, use_configuration
    from apps.harvest.sources import resolve_source
    from apps.model.models import SourceIdentity

    original = load_configuration()
    rows = [{"code": "scb", "base_api_url": "https://example.org/api/v2", "timeout": 0}]
    override = legacy_configuration(original, "pxweb_v2", rows)
    with use_configuration(override):
        _, service = resolve_source(
            SourceIdentity(
                adapter="pxweb_v2", source_code="scb", dataset_code="T1", language="sv"
            ),
            None,
            {},
        )
        assert service.endpoint_url == ["https://example.org/api/v2"]
        row = next(p for p in providers_for_adapter("pxweb_v2") if p["code"] == "scb")
        assert row["timeout"] == 0
    assert load_configuration() is original

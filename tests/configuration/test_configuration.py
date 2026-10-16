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


def test_profile_upgrade_keeps_payloads_ids_and_unattributed_records():
    from apps.migrations.profile_reader import read_dataset, upgrade_document
    from apps.model.vocabulary import PROFILE1_CONTEXT
    from apps.parsing.contributions import convert_dataset, legacy_document
    from tests.model.test_statdcat import legacy

    native = legacy()
    current = convert_dataset(native, adapter="pxweb_v2").dataset.to_jsonld()
    previous = json.loads(json.dumps(current))
    previous["ni:profileVersion"] = "1.0.0"
    previous["@context"] = PROFILE1_CONTEXT
    previous["dct:publisher"] = [
        {
            "@type": "foaf:Agent",
            "foaf:name": {"sv": ["Publisher"]},
            "foaf:page": ["https://example.org/"],
        }
    ]
    dist = previous["dcat:distribution"][0]
    dist["ni:retrieval"]["source"]["provider_code"] = dist["ni:retrieval"][
        "source"
    ].pop("source_code")
    dist["dcat:accessService"][0]["@id"] = "urn:nordicintel:service:pxweb_v2"
    dist.setdefault("ni:additionalAttributes", {})["custom"] = {
        "unfamiliar": {"empty": [], "zero": 0}
    }
    upgraded, ledger = upgrade_document(previous)
    assert upgraded["@id"] == previous["@id"]
    assert upgraded["dcat:distribution"][0]["ni:structure"] == dist["ni:structure"]
    assert (
        upgraded["dcat:distribution"][0]["ni:additionalAttributes"]
        == dist["ni:additionalAttributes"]
    )
    parsed = read_dataset(previous)
    assert parsed.profile_version == "2.0.0"
    assert parsed.publisher[0].homepage == ["https://example.org/"]
    assert not parsed.publisher[0].additional_attributes
    assert ledger["services"][0]["old"] == ["urn:nordicintel:service:pxweb_v2"]
    restored = legacy_document(parsed.distributions[0], parsed)
    assert restored == native


def test_unattributed_refresh_preserves_findings_across_languages():
    from apps.model.composition import assemble
    from apps.parsing.contributions import convert_dataset
    from tests.model.test_statdcat import legacy

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


def test_offline_profile_migration_retains_publication_and_codelist_bytes(tmp_path):
    import gzip
    from importlib.util import module_from_spec, spec_from_file_location

    from apps.catalog.assembly import build
    from apps.parsing.accepted import write_metadata
    from apps.publication.candidates import promote
    from tests.model.test_statdcat import legacy
    from tests.publication.test_catalog import unpack

    spec = spec_from_file_location("migrate_profile2", "apps/migrations/profile2.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    data = tmp_path / "data"
    write_metadata(data / "pxweb_v2", [legacy()], adapter="pxweb_v2")
    candidate = tmp_path / "catalog-candidate"
    build(data, candidate, adapters=["pxweb_v2"])
    promote(data, candidate)
    before = (data / "catalog/codelists.jsonl.zst").read_bytes()
    output = tmp_path / "migration"
    summary = migration.convert(data, output)
    assert summary["accepted_datasets"] == summary["published_datasets"] == 1
    assert (output / "data/catalog/codelists.jsonl.zst").read_bytes() == before
    original = json.loads(unpack(data / "catalog/catalog.jsonl.zst"))
    migrated = json.loads(unpack(output / "data/catalog/catalog.jsonl.zst"))
    assert migrated["@id"] == original["@id"]
    assert migrated["dcat:distribution"] == original["dcat:distribution"]
    ledger = json.loads(
        gzip.decompress((output / "audit/publication.jsonl.gz").read_bytes())
    )
    assert ledger["dataset"] == original["@id"]

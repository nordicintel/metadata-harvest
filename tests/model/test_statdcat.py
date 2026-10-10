"""StatDCAT metadata preserves meaning and unknown source attributes."""

import json
from copy import deepcopy

import pytest


def legacy(language="sv", code="T1", provider="scb"):
    return {
        "class": "dataset",
        "version": "2.0",
        "label": "Table " + language,
        "id": ["Region", "Tid"],
        "size": [2, 2],
        "value": [],
        "role": {"geo": ["Region"], "time": ["Tid"]},
        "dimension": {
            "Region": {
                "label": "Region",
                "category": {
                    "index": {"01": 0, "00": 1},
                    "label": {"01": "One", "00": "All"},
                    "unit": {"01": {"label": "people", "decimals": 0}},
                    "custom": {"01": {"flag": False}},
                },
                "extension": {
                    "nordicintel": {"elimination": False},
                    "pxweb_v2": {"new": 0},
                },
            },
            "Tid": {"label": "Time", "category": {"index": ["2020", "2021"]}},
        },
        "extension": {
            "nordicintel": {
                "provider_code": provider,
                "dataset_code": code,
                "language": language,
                "first_period": "2020",
                "last_period": "2021",
                "time_unit": "annual",
                "official_statistics": True,
                "discontinued": False,
                "data_url": "https://example.org/data",
                "metadata_url": "https://example.org/meta",
                "new_information": {"zero": 0, "empty": [], "false": False},
            },
            "pxweb_v2": {"max_cells": 1000, "source_record": {"id": code}},
        },
    }


def test_models_preserve_nested_custom_fields_and_reject_boolean_coercion():
    from apps.model.models import Dataset, Dimension

    dimension = Dimension(code="01", label={"sv": ["Region"]}, future={"zero": 0})
    model = Dataset(
        id="urn:test:1",
        title={"sv": ["Example"]},
        official_statistics=True,
        discontinued=False,
        dimensions=[dimension],
        future={"nested": [False, 0, ""]},
    )
    encoded = model.to_jsonld()
    assert encoded["ni:officialStatistics"] is True
    assert encoded["ni:discontinued"] is False
    assert encoded["ni:additionalAttributes"]["custom"]["future"] == {
        "nested": [False, 0, ""]
    }
    restored = Dataset.from_jsonld(encoded)
    assert restored.to_jsonld() == encoded
    assert restored.dimensions[0].additional_attributes["custom"]["future"] == {
        "zero": 0
    }
    with pytest.raises(ValueError):
        Dataset(id="urn:test:1", official_statistics="false")


def test_conversion_maps_periods_and_preserves_unknowns_without_parallel_fields():
    from apps.harvest.contributions import convert_dataset, legacy_document

    source = legacy()
    original = deepcopy(source)
    contribution = convert_dataset(source, adapter="pxweb_v2")
    dataset = contribution.dataset
    encoded = dataset.to_jsonld()
    assert source == original
    assert dataset.temporal[0].start_date == "2020-01-01"
    assert dataset.temporal[0].end_date == "2021-12-31"
    assert dataset.temporal_resolution == ["P1Y"]
    assert "first_period" not in json.dumps(encoded)
    assert "last_period" not in json.dumps(encoded)
    assert "time_unit" not in json.dumps(encoded)
    assert dataset.official_statistics is True
    assert dataset.discontinued is False
    assert (
        dataset.additional_attributes["nordicintel"]["new_information"]["false"]
        is False
    )
    reconstructed = legacy_document(dataset.distributions[0])
    assert reconstructed["id"] == source["id"]
    assert reconstructed["dimension"] == source["dimension"]
    assert reconstructed["extension"]["pxweb_v2"] == source["extension"]["pxweb_v2"]
    assert len(contribution.codelists) == 2


def test_language_and_kolada_scope_assembly_keeps_distinct_bindings():
    from apps.harvest.contributions import convert_dataset
    from apps.model.composition import assemble

    parts = []
    for language, code in [
        ("sv", "municipality/N00003"),
        ("en", "municipality/N00003"),
        ("sv", "ou/N00003"),
    ]:
        doc = legacy(language, code, "kolada")
        doc["extension"]["kolada"] = {"scope": code.split("/")[0], "kpi_id": "N00003"}
        doc["extension"].pop("pxweb_v2")
        parts.append(convert_dataset(doc, adapter="kolada"))
    datasets, codes = assemble(parts)
    assert len(datasets) == 1
    assert set(datasets[0].title) == {"sv", "en"}
    assert len(datasets[0].distributions) == 3
    assert {d.retrieval.source.dataset_code for d in datasets[0].distributions} == {
        "municipality/N00003",
        "ou/N00003",
    }
    assert len({c.id for c in codes}) == len(codes)


def test_unrepresentable_period_and_conflicting_flags_survive():
    from apps.harvest.contributions import convert_dataset
    from apps.model.composition import assemble

    doc = legacy()
    doc["extension"]["nordicintel"]["first_period"] = "1992-1998"
    first = convert_dataset(doc, adapter="pxweb_v2")
    assert (
        first.dataset.additional_attributes["nordicintel"]["first_period"]
        == "1992-1998"
    )
    second_doc = legacy("en")
    second_doc["extension"]["nordicintel"]["official_statistics"] = False
    second = convert_dataset(second_doc, adapter="pxweb_v2")
    datasets, _ = assemble([first, second])
    assert datasets[0].official_statistics is None
    assert {d.official_statistics for d in datasets[0].distributions} == {True, False}


def test_software_services_are_separate_from_scb_owner_and_catalogs():
    from apps.harvest.contributions import convert_dataset

    main = legacy()
    main["extension"]["nordicintel"]["data_url"] = (
        "https://statistikdatabasen.scb.se/api/v2/tables/T1/data"
    )
    hvd = legacy(provider="scb_hvd")
    hvd["extension"]["nordicintel"]["data_url"] = (
        "https://hvduppdrag.scb.se/api/v1/sv/uppdrag/T1"
    )
    hvd["extension"]["pxweb_v1"] = hvd["extension"].pop("pxweb_v2")
    a = convert_dataset(main, adapter="pxweb_v2").dataset
    b = convert_dataset(hvd, adapter="pxweb_v1").dataset
    assert a.identifiers == b.identifiers == ["T1"]
    assert (
        a.catalogs[0].publisher[0].id
        == b.catalogs[0].publisher[0].id
        == "urn:nordicintel:agent:scb"
    )
    assert a.catalogs[0].id != b.catalogs[0].id
    assert a.id != b.id
    assert a.distributions[0].services[0].adapter == "pxweb_v2"
    assert b.distributions[0].services[0].adapter == "pxweb_v1"
    assert (
        a.distributions[0].services[0].id
        == "urn:nordicintel:service:scb:pxweb_v2:statistics"
    )
    assert (
        b.distributions[0].services[0].id == "urn:nordicintel:service:scb:pxweb_v1:hvd"
    )
    assert b.distributions[0].retrieval.source.provider_code == "scb_hvd"


def test_jsonld_expansion_preserves_extensions_and_codelist_membership():
    from rdflib import Graph, URIRef

    from apps.harvest.contributions import convert_dataset
    from apps.model.vocabulary import NAMESPACE

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    dataset.new_field = {"nested": {"false": False, "zero": 0}}
    graph = Graph().parse(data=json.dumps(dataset.to_jsonld()), format="json-ld")
    subject = URIRef(dataset.id)
    assert list(graph.objects(subject, URIRef(NAMESPACE + "additionalAttributes")))
    scheme = dataset.distributions[0].structure.components[0].dimension.code_list
    assert list(
        graph.subjects(
            URIRef("http://www.w3.org/2004/02/skos/core#inScheme"), URIRef(scheme.id)
        )
    )


def test_invalid_source_hierarchy_is_rejected():
    from apps.harvest.contributions import convert_dataset

    doc = legacy()
    doc["dimension"]["Region"]["category"]["child"] = {"01": ["00"], "00": ["01"]}
    with pytest.raises(ValueError):
        convert_dataset(doc, adapter="pxweb_v2")


def test_codelist_attribute_conflicts_remain_json_and_keep_every_assertion():
    from apps.model.composition import merge_codelists
    from apps.model.models import CodeList, Concept

    schemes = [
        CodeList(
            id="urn:scheme",
            codes=[
                Concept(
                    code="01",
                    unit={"label": unit},
                    additional_attributes={"source": {"future": value}},
                )
            ],
        )
        for unit, value in [("a", False), ("b", 0), ("c", [])]
    ]
    merged = merge_codelists(merge_codelists(schemes[0], schemes[1]), schemes[2])
    serialized = merged.to_jsonld()
    assert CodeList.from_jsonld(serialized).to_jsonld() == serialized
    assert merged.codes[0].additional_attributes["conflicts"]["unit"] == [
        {"label": "a"},
        {"label": "b"},
        {"label": "c"},
    ]
    assert schemes[0].codes[0].additional_attributes == {"source": {"future": False}}


def test_codelist_merge_preserves_language_specific_category_order():
    from apps.harvest.contributions import convert_codelist
    from apps.model.composition import merge_codelists
    from apps.model.models import CodeList

    schemes = [
        convert_codelist(
            {
                "provider_code": "scb",
                "language": language,
                "codelist": {
                    "id": "native",
                    "label": "Codes",
                    "values": [{"code": code, "label": code} for code in codes],
                },
            }
        )
        for language, codes in [("sv", ["02", "01"]), ("en", ["01", "02"])]
    ]
    merged = merge_codelists(*schemes)
    assert CodeList.from_jsonld(merged.to_jsonld()).category_order == {
        "sv": ["02", "01"],
        "en": ["01", "02"],
    }


def test_every_configured_source_has_an_explicit_service_and_owner_disposition():

    from apps.configuration import providers_for_adapter
    from apps.harvest.registry import ADAPTERS
    from apps.harvest.sources import deployment, inventory

    configured = set()
    for adapter in ADAPTERS:
        for source in providers_for_adapter(adapter):
            configured.add((adapter, source["code"]))
            item = deployment(adapter, source["code"])
            assert item["endpoint"]
            assert item["software"]
            assert item["owner"] or item["owner_status"] == "record_specific"
    assert configured == {(d["adapter"], d["source_code"]) for d in inventory()}
    for a, p, b, q in [
        ("pxweb_v1", "konj", "pxweb_v1", "konjbar"),
        ("pxweb_v1", "skog", "pxweb_v1", "skog_ska22"),
        ("sdb", "socialstyrelsen", "dataanalys", "socialstyrelsen_analys"),
        ("pxweb_v1", "goteborg", "entrysoft", "cataloggoteborgse"),
        ("pxweb_v1", "linkoping", "entrysoft", "linkopingentryscapenet"),
        ("pxweb_v1", "vgregion", "entrysoft", "vgregionentryscapenet"),
        ("pxweb_v1", "lansstyr", "entrysoft", "cataloglansstyrelsense"),
    ]:
        left, right = deployment(a, p), deployment(b, q)
        assert left["owner"]["code"] == right["owner"]["code"]
        assert (a, p) != (b, q)


def test_riksbank_api_products_and_pxweb_databases_keep_distinct_catalogs():
    from apps.harvest.sources import source_entities
    from apps.model.models import SourceIdentity

    def entities(adapter, provider, url, record):
        return source_entities(
            SourceIdentity(
                adapter=adapter,
                provider_code=provider,
                dataset_code="T1",
                language="sv",
            ),
            url,
            record,
        )

    first = entities(
        "pxweb_v1",
        "linkoping",
        "https://statistik.linkoping.se/pxweb/api/v1/sv/A/T1",
        {"database_id": "A"},
    )
    second = entities(
        "pxweb_v1",
        "linkoping",
        "https://statistik.linkoping.se/pxweb/api/v1/sv/B/T1",
        {"database_id": "B"},
    )
    assert first[0].id != second[0].id
    assert first[1].id == second[1].id
    swea = entities(
        "riksbank", "riksbank", "https://api.riksbank.se/swea/v1/Observations/T1", {}
    )
    policy = entities(
        "riksbank",
        "riksbank",
        "https://api.riksbank.se/monetary_policy_data/v1/forecasts",
        {},
    )
    assert (
        swea[1].id
        == policy[1].id
        == "urn:nordicintel:service:riksbank:riksbank:statistics"
    )
    assert swea[1].to_jsonld() == policy[1].to_jsonld()
    assert swea[0].publisher[0].id == policy[0].publisher[0].id


def test_service_identity_depends_on_binding_and_description_on_adapter():
    from apps.harvest.sources import source_entities
    from apps.model.models import SourceIdentity

    services = []
    for provider in ("scb_hvd", "csn", "konj", "konjbar", "linkoping"):
        source = SourceIdentity(
            adapter="pxweb_v1", provider_code=provider, dataset_code="T1", language="sv"
        )
        services.append(source_entities(source, "https://example.org/data", {})[1])
    assert len({s.id for s in services}) == 5
    assert len({json.dumps(s.description, sort_keys=True) for s in services}) == 1
    assert services[0].publisher[0].id == "urn:nordicintel:agent:scb"
    assert services[0].endpoint_url == ["https://hvduppdrag.scb.se/api/v1"]


def test_distribution_retrieval_selects_adapter_not_owner(monkeypatch):
    import asyncio

    from apps.harvest.contributions import convert_dataset
    from retrieval import retrieve_distribution
    from retrieval.data_adapters import pxweb_v1 as v1

    doc = legacy(provider="scb_hvd")
    doc["extension"]["pxweb_v1"] = doc["extension"].pop("pxweb_v2")
    distribution = convert_dataset(doc, adapter="pxweb_v1").dataset.distributions[0]
    calls = []

    async def retrieve(session, **kwargs):
        calls.append(kwargs)
        return {"value": [0]}

    monkeypatch.setattr(v1, "retrieve", retrieve)
    assert asyncio.run(
        retrieve_distribution(
            object(),
            distribution.to_jsonld(),
            selection={"Region": ["00"], "Tid": ["2021"]},
        )
    ) == {"value": [0]}
    assert calls[0]["provider_code"] == "scb_hvd"
    assert calls[0]["metadata"]["/extension/pxweb_v1"]["max_cells"] == 1000
    with pytest.raises(ValueError):
        asyncio.run(
            retrieve_distribution(
                object(),
                distribution.to_jsonld(),
                selection={"Region": ["missing"], "Tid": ["2021"]},
            )
        )


def test_compatibility_projection_keeps_descriptive_and_optional_values():
    from apps.harvest.contributions import convert_dataset, legacy_document

    doc = legacy()
    doc.update(updated="2026-10-01", note=["A note"], source="Attribution")
    doc["extension"]["nordicintel"].update(
        contacts=[
            {
                "name": "A",
                "email": "a@example.org",
                "organization": "Org",
                "custom": False,
            }
        ],
        subject={"code": "X", "label": "Topic"},
        next_release="2026-11-01",
        source_url="https://example.org/table",
        doc_url="https://example.org/docs",
    )
    converted = convert_dataset(doc, adapter="pxweb_v2").dataset
    projected = legacy_document(converted.distributions[0], converted)
    assert projected == doc


def test_services_are_concrete_and_shared_by_distributions_of_that_api():
    from apps.configuration import load_configuration
    from apps.harvest.sources import concrete_service

    services = [
        concrete_service(binding) for binding in load_configuration().services.values()
    ]
    assert len({s.id for s in services}) == 83
    assert len({s.adapter for s in services}) == 12
    assert all(s.endpoint_url for s in services)


def test_all_entryscape_owners_use_canonical_organisation_codes():
    from apps.harvest.sources import inventory, source_entities
    from apps.model.models import SourceIdentity

    items = inventory()
    entry = [item for item in items if item["adapter"] == "entrysoft"]
    assert len(entry) == 47
    for item in entry:
        owner = item["owner"]
        if owner is None:
            assert item["source_code"] == "freeentryscapecom"
            continue
        assert not owner["code"].startswith(("catalog", "katalog", "opendata"))
        assert "entryscape" not in owner["code"]
        catalog, service = source_entities(
            SourceIdentity(
                adapter="entrysoft",
                provider_code=item["source_code"],
                dataset_code="01",
                language="sv",
            ),
            None,
            {},
        )
        assert catalog.publisher[0].id == "urn:nordicintel:agent:" + owner["code"]
        assert item["source_code"] not in catalog.id
        assert service.id == item["service_id"]
    both = {item["owner"]["code"] for item in entry if item["owner"]} & {
        item["owner"]["code"] for item in items if item["adapter"] == "pxweb_v1"
    }
    assert both == {"goteborg", "linkoping", "vgregion", "lansstyr"}
    for owner in both:
        catalogs = []
        for item in items:
            if item["owner"] and item["owner"]["code"] == owner:
                c, _ = source_entities(
                    SourceIdentity(
                        adapter=item["adapter"],
                        provider_code=item["source_code"],
                        dataset_code="01",
                        language="sv",
                    ),
                    None,
                    {},
                )
                catalogs.append(c)
        assert len({c.publisher[0].id for c in catalogs}) == 1
        assert len({c.id for c in catalogs}) == 2


def test_missing_description_is_a_quality_finding_not_an_identity_error():
    from apps.catalog.quality import validate_publication
    from apps.harvest.contributions import convert_dataset

    model = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    assert any(
        finding["code"] == "missing_description"
        for finding in validate_publication(model)
    )


def test_malformed_optional_contacts_preserve_source_values():
    from apps.harvest.contributions import convert_dataset, legacy_document

    for contacts in (
        "free text",
        [{"name": "Contact", "phone": ["01", "02"], "empty": None}],
    ):
        document = legacy()
        document["extension"]["nordicintel"]["contacts"] = contacts
        model = convert_dataset(document, adapter="pxweb_v2").dataset
        assert model.distributions[0].source_evidence["diagnostics"]
        assert legacy_document(model.distributions[0], model) == document


@pytest.mark.parametrize(
    "adapter",
    [
        "pxweb_v1",
        "pxweb_v2",
        "kolada",
        "uka",
        "sdb",
        "trafa",
        "entrysoft",
        "smhi_emissions",
        "forsakringskassan",
        "riksbank",
        "dataanalys",
        "tillvaxtverket",
    ],
)
def test_every_public_adapter_returns_typed_contributions(adapter, monkeypatch):
    import asyncio
    import importlib

    from apps.model.models import HarvestContribution

    suffix = "metadata" if adapter in {"pxweb_v1", "pxweb_v2", "kolada"} else "workflow"
    native = importlib.import_module(
        f"apps.harvest.metadata_adapters.{adapter}.{suffix}"
    )
    document = legacy(provider="p")
    document["extension"][adapter] = document["extension"].pop("pxweb_v2")

    async def fetch(*args, **kwargs):
        return document

    monkeypatch.setattr(
        native,
        "get_pxweb_metadata" if adapter == "pxweb_v1" else "fetch_metadata",
        fetch,
    )
    public = importlib.import_module(f"apps.harvest.metadata_adapters.{adapter}")
    kwargs = {"provider_code": "p"} if adapter == "pxweb_v1" else {"client": object()}
    parts = asyncio.run(public.fetch_metadata({}, **kwargs))
    assert len(parts) == 1 and isinstance(parts[0], HarvestContribution)
    assert parts[0].source.adapter == adapter
    assert parts[0].dataset.distributions[0].services[0].adapter == adapter

"""Portal snapshots are accepted atomically; matching cannot use generic endpoints."""

import json
from copy import deepcopy

import pytest

from tests.support.worker_document import legacy


def record(identifier="T1", publisher="Statistiska centralbyrån", url=None):
    return {
        "id": "portal-" + identifier,
        "resource": "http://data.europa.eu/88u/dataset/portal-" + identifier,
        "catalog": {"id": "oppnadata"},
        "identifier": [identifier],
        "publisher": {"name": publisher},
        "title": {"sv": "Portal title"},
        "description": {"sv": "Portal description"},
        "distributions": [
            {
                "id": "d-" + identifier,
                "access_url": [
                    url or "https://api.scb.se/OV0104/v1/doris/sv/ssd/" + identifier
                ],
                "license": {
                    "resource": "http://creativecommons.org/publicdomain/zero/1.0/"
                },
            }
        ],
        "future": {"important": False},
    }


def test_import_is_atomic_and_rejects_partial_or_wrong_catalog(tmp_path):
    from apps.portal.snapshot import import_snapshot

    source = tmp_path / "input.jsonl"
    target = tmp_path / "accepted"
    source.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    import_snapshot(source, target)
    previous = (target / "oppnadata.jsonl.gz").read_bytes()
    source.write_text(
        json.dumps({**record(), "catalog": {"id": "other"}}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        import_snapshot(source, target)
    assert (target / "oppnadata.jsonl.gz").read_bytes() == previous


def test_match_is_scoped_and_ambiguous_identifiers_do_not_merge():
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    match = PortalIndex(
        [record(), record("T1", "Other publisher") | {"id": "other"}]
    ).match(dataset)
    assert match.status == "matched"
    assert match.record_id == "portal-T1"
    second = record() | {"id": "duplicate"}
    assert PortalIndex([record(), second]).match(dataset).status == "ambiguous"
    generic = "https://example.org/data"
    assert (
        PortalIndex([record("A", url=generic), record("B", url=generic)])
        .match(dataset)
        .status
        == "unmatched"
    )


def test_enrichment_preserves_metadata_and_scb_filter_is_reversible():
    from apps.catalog.enrichment import enrich
    from apps.catalog.filtering import publication_view
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    original = deepcopy(dataset.to_jsonld())
    index = PortalIndex([record()])
    enriched = enrich(dataset, index)
    assert dataset.to_jsonld() == original
    assert enriched.title["sv"] == ["Table sv"]
    assert enriched.description["sv"] == ["Portal description"]
    assert len(enriched.distributions) == 2
    assert len(publication_view(enriched).distributions) == 1
    assert len(publication_view(enriched, include_scb_pxweb_v1=True).distributions) == 2
    assert enriched.additional_attributes["data_eu"]["future"]["important"] is False


def test_hvd_and_other_pxweb_v1_services_are_not_filtered():
    from apps.catalog.filtering import publication_view
    from apps.parsing.contributions import convert_dataset

    doc = legacy(provider="scb_hvd")
    doc["extension"]["nordicintel"]["data_url"] = (
        "https://hvduppdrag.scb.se/api/v1/sv/uppdrag/T1"
    )
    doc["extension"]["pxweb_v1"] = doc["extension"].pop("pxweb_v2")
    assert (
        len(
            publication_view(
                convert_dataset(doc, adapter="pxweb_v1").dataset
            ).distributions
        )
        == 1
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/data",
        "https://example.org/api/v1/",
        "https://example.org/custom-root",
    ],
)
def test_singleton_service_endpoints_never_establish_identity(url):
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    dataset.distributions[0].access_url = [url]
    portal = record("unrelated", "Unrelated owner", url)
    if url.endswith("custom-root"):
        portal["distributions"][0]["access_service"] = [{"endpoint_url": [url]}]
    assert PortalIndex([portal]).match(dataset).status == "unmatched"


def test_filter_removes_references_to_removed_distribution_structures():
    from apps.catalog.filtering import publication_view
    from apps.catalog.quality import validate_publication
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    dataset.distributions[0].access_url = [
        "https://api.scb.se/OV0104/v1/doris/sv/ssd/T1"
    ]
    filtered = publication_view(dataset)
    validate_publication(filtered)
    assert filtered.structure is None
    assert filtered.dimensions == []
    assert publication_view(dataset, include_scb_pxweb_v1=True) == dataset


def test_portal_nested_unknown_properties_survive_enrichment():
    from apps.catalog.enrichment import enrich
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    portal = record()
    portal["categories"] = [
        {
            "resource": "https://example.org/theme",
            "id": "T",
            "label": {"sv": "Tema"},
            "future": {"keep": False},
        }
    ]
    portal["distributions"][0]["license"]["future"] = {"keep": 0}
    enriched = enrich(
        convert_dataset(legacy(), adapter="pxweb_v2").dataset, PortalIndex([portal])
    )
    assert enriched.themes[0].additional_attributes["data_eu"]["future"] == {
        "keep": False
    }
    assert enriched.distributions[-1].source_assertions["licenseAttributes"][0][
        "attributes"
    ]["future"] == {"keep": 0}
    assert "future" in json.dumps(enriched.to_jsonld())


def test_linked_resource_attributes_and_secondary_labels_survive():
    from apps.catalog.enrichment import enrich
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    portal = record()
    portal["landing_page"] = [
        {"resource": "https://example.org/table/T1", "future": {"keep": False}}
    ]
    portal["distributions"][0]["access_url"] = [
        {"resource": "https://example.org/table/T1/data", "future": 0}
    ]
    portal["language"] = [
        {
            "resource": "http://example.org/sv",
            "label": {"sv": "Svenska"},
            "title": "Original title",
        }
    ]
    result = enrich(
        convert_dataset(legacy(), adapter="pxweb_v2").dataset, PortalIndex([portal])
    )
    assert result.source_assertions["landing_pageAttributes"][0]["attributes"][
        "future"
    ] == {"keep": False}
    assert (
        result.distributions[-1].source_assertions["access_urlAttributes"][0][
            "attributes"
        ]["future"]
        == 0
    )
    assert (
        result.languages[0].additional_attributes["data_eu"]["title"]
        == "Original title"
    )


@pytest.mark.parametrize(
    "provider,url",
    [
        ("csn", "https://statistik.csn.se:443/PXWeb/api/v1/sv/CSNstat/AB/ABAKD11.px"),
        (
            "domstol",
            "http://pxweb.etjanst.domstol.se/PxWeb/api/v1/sv/DOMstat/AB/ABAKD11.px",
        ),
    ],
)
def test_configured_pxweb_urls_supply_provider_scoped_native_ids(provider, url):
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    doc = legacy(provider=provider, code="ABAKD11.px")
    doc["extension"]["pxweb_v1"] = doc["extension"].pop("pxweb_v2")
    dataset = convert_dataset(doc, adapter="pxweb_v1").dataset
    portal = record("portal-generated-slug", "Publisher spelling differs", url)
    portal["identifier"] = None
    match = PortalIndex([portal]).match(dataset)
    assert match.status == "matched"
    assert match.method == "identifier"
    assert (
        PortalIndex([portal, portal | {"id": "another"}]).match(dataset).status
        == "ambiguous"
    )
    portal["distributions"][0]["access_url"] = [
        "https://unrelated.example/api/v1/sv/db/ABAKD11.px"
    ]
    assert PortalIndex([portal]).match(dataset).status == "unmatched"


def test_identifier_matching_normalizes_encoding_case_whitespace_and_punctuation():
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(code="AB%20-01.px"), adapter="pxweb_v2").dataset
    portal = record("ab_01.px")
    assert PortalIndex([portal]).match(dataset).status == "matched"
    assert dataset.identifiers == ["AB%20-01.px"]
    alternate = record("AB -01.px")
    match = PortalIndex([portal, alternate]).match(dataset)
    assert match.status == "ambiguous"
    assert len(match.candidates) == 2
    assert (
        PortalIndex([portal | {"publisher": {"name": "Another owner"}}])
        .match(dataset)
        .status
        == "unmatched"
    )


def test_portal_pxweb_query_endpoint_is_access_not_a_file_download():
    from apps.catalog.enrichment import enrich
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    portal = record()
    endpoint = "http://statistik.csn.se/PxWeb/api/v1/sv/CSNstat/AB/T1.px"
    portal["distributions"][0]["access_url"] = []
    portal["distributions"][0]["download_url"] = [endpoint]
    distribution = enrich(dataset, PortalIndex([portal])).distributions[-1]
    assert distribution.access_url == [endpoint]
    assert distribution.download_url == []
    assert (
        distribution.services[0].id == "urn:nordicintel:service:csn:pxweb_v1:statistics"
    )


def test_distinct_dataset_page_fragments_do_not_match():
    from apps.catalog.matching import PortalIndex
    from apps.parsing.contributions import convert_dataset

    dataset = convert_dataset(legacy(), adapter="pxweb_v2").dataset
    dataset.landing_page = ["https://example.org/table-pages#A"]
    portal = record("different")
    portal["landing_page"] = ["https://example.org/table-pages#B"]
    assert PortalIndex([portal]).match(dataset).status == "unmatched"

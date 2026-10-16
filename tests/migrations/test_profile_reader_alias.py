"""Renamed adapter tokens are aliased in identifiers only, never in content."""

from apps.migrations.profile_reader import _rename_adapters


def test_rename_touches_identifiers_and_namespaces_but_not_source_text():
    document = {
        "@id": "urn:nordicintel:dataset:ale:rowstore:x",
        "dct:title": {"sv": ["Tabell om entrysoft-leverantören"]},
        "dcat:accessURL": ["https://catalog.ale.se/rowstore/entrysoft"],
        "ni:retrieval": {
            "source": {"adapter": "entrysoft", "source_code": "catalogalese"},
            "options": {"entrysoft": {"rowcount": 1}},
        },
        "dcat:accessService": [
            {"@id": "urn:nordicintel:service:ale:entrysoft:open-data"}
        ],
        "ni:additionalAttributes": {"entrysoft:fields": {"a": 1}},
        "ni:sourceEvidence": {"note": "entrysoft"},
        "ni:structure": {
            "id": "urn:nordicintel:distribution:ale:entrysoft:x:sv:structure"
        },
    }
    renamed = _rename_adapters(document)
    assert renamed["dct:title"] == document["dct:title"]
    assert renamed["dcat:accessURL"] == document["dcat:accessURL"]
    assert renamed["ni:sourceEvidence"] == {"note": "entrysoft"}
    assert renamed["ni:retrieval"]["source"]["adapter"] == "entryscape"
    assert renamed["ni:retrieval"]["options"] == {"entryscape": {"rowcount": 1}}
    assert renamed["dcat:accessService"][0]["@id"] == (
        "urn:nordicintel:service:ale:entryscape:open-data"
    )
    assert renamed["ni:additionalAttributes"] == {"entryscape:fields": {"a": 1}}
    assert renamed["ni:structure"]["id"] == (
        "urn:nordicintel:distribution:ale:entryscape:x:sv:structure"
    )

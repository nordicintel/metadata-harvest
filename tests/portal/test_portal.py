"""Portal snapshots are accepted atomically; matching cannot use generic endpoints."""

import json

import pytest


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

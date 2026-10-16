"""Accepted metadata is multilingual StatDCAT; parsing retains task identities."""

import gzip
import json

from tests.model.test_statdcat import legacy


def test_accepted_collection_combines_languages_and_round_trips_task_records(tmp_path):
    from apps.parsing.accepted import load_contributions, write_metadata

    documents = [legacy("sv"), legacy("en")]
    write_metadata(tmp_path, documents, adapter="pxweb_v2")
    paths = list((tmp_path / "metadata").glob("*.gz"))
    assert len(paths) == 1
    with gzip.open(paths[0], "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    assert len(records) == 1
    assert records[0]["@type"] == "dcat:Dataset"
    assert len(records[0]["dcat:distribution"]) == 2
    restored = list(load_contributions(tmp_path / "metadata"))
    assert {d["extension"]["nordicintel"]["language"] for d in restored} == {"sv", "en"}
    for doc in restored:
        assert doc == legacy(doc["extension"]["nordicintel"]["language"])


def test_accepted_scopes_do_not_erase_each_other(tmp_path):
    from apps.parsing.accepted import load_contributions, write_metadata

    docs = []
    for scope in ["municipality", "ou"]:
        doc = legacy(code=scope + "/N1", provider="kolada")
        doc["extension"]["kolada"] = {"scope": scope, "kpi_id": "N1"}
        doc["extension"].pop("pxweb_v2")
        docs.append(doc)
    write_metadata(tmp_path, docs, adapter="kolada")
    saved = list(load_contributions(tmp_path / "metadata"))
    saved[0]["label"] = "Updated"
    write_metadata(tmp_path, saved, adapter="kolada")
    assert len(list(load_contributions(tmp_path / "metadata"))) == 2

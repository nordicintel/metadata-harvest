"""Per-dataset source files and the adapter manifest that indexes them."""

import json
from pathlib import Path

import pytest

from apps.harvest.source_store import (
    Rejected,
    SourceStore,
    harvest_manifest,
    source_filename,
    source_key,
    split_key,
)

STAMP = "2026-10-16T05:00:00+00:00"


def test_source_filename_is_stable_safe_and_unique():
    assert source_filename("ABAKD11.px", "sv") == "ABAKD11.px.sv.json"
    assert (
        source_filename("inkvartering/Gäster", "sv")
        == "inkvartering%2FG%C3%A4ster.sv.json"
    )
    a, b = (source_filename("x" * 70 + s, "en") for s in ("1", "2"))
    assert a != b
    assert len(a) <= 65 + 15 + len(".en.json")
    assert "/" not in a
    assert source_filename("t", "sv", part="labels.csv") == "t.sv.labels.csv"


def test_split_key_round_trips_codes_with_slashes():
    key = source_key("fk", "syn-kommun/Tabell 1", "sv")
    assert key == "fk/syn-kommun/Tabell 1/sv"
    assert split_key(key) == ("fk", "syn-kommun/Tabell 1", "sv")


def _store(root: Path) -> SourceStore:
    return SourceStore(root / "source", "trafa")


def test_put_get_save_round_trip_is_deterministic(tmp_path):
    store = _store(tmp_path)
    key = source_key("trafa", "t0801|ar", "sv")
    store.put(key, responses={"probe": {"a": 1}}, updated=None, retrieved_at=STAMP)
    store.save()
    manifest = tmp_path / "source/manifest.json"
    before = {p: p.read_bytes() for p in (tmp_path / "source").rglob("*.json")}
    assert manifest in before
    loaded = _store(tmp_path)
    assert loaded.entries() == {
        key: {
            "files": ["trafa/t0801%7Car.sv.json"],
            "updated": None,
            "retrieved_at": STAMP,
        }
    }
    envelope = loaded.get(key)
    assert envelope == {
        "key": key,
        "identifier": "t0801|ar",
        "language": "sv",
        "updated": None,
        "retrieved_at": STAMP,
        "responses": {"probe": {"a": 1}},
        "derived": {},
    }
    loaded.put(key, responses={"probe": {"a": 1}}, updated=None, retrieved_at=STAMP)
    loaded.save()
    assert {p: p.read_bytes() for p in (tmp_path / "source").rglob("*.json")} == before
    assert loaded.get("trafa/missing/sv") is None


def test_put_rejects_empty_and_unserialisable_bodies(tmp_path):
    store = _store(tmp_path)
    key = source_key("trafa", "t1", "sv")
    store.put(key, responses={"probe": {"a": 1}}, updated=None, retrieved_at=STAMP)
    store.save()
    path = tmp_path / "source/trafa/t1.sv.json"
    before = path.read_bytes()
    manifest_before = (tmp_path / "source/manifest.json").read_bytes()
    store = _store(tmp_path)
    for bad in ({}, None, "", [], {"x": float("nan")}):
        with pytest.raises(Rejected):
            store.put(key, responses={"probe": bad}, updated=None, retrieved_at="later")
    with pytest.raises(Rejected):
        store.put(key, responses={}, updated=None, retrieved_at="later")
    with pytest.raises(Rejected):
        store.put_shared("trafa", "listing.json", None, retrieved_at="later")
    store.save()
    assert path.read_bytes() == before
    assert (tmp_path / "source/manifest.json").read_bytes() == manifest_before
    assert store.entries()[key]["retrieved_at"] == STAMP


def test_save_keeps_shared_files_and_untouched_entries(tmp_path):
    store = _store(tmp_path)
    store.put_shared("trafa", "products.json", [{"id": 1}], retrieved_at=STAMP)
    store.put_shared("trafa", "documentation.T1.html", "<p>x</p>", retrieved_at=STAMP)
    a = source_key("trafa", "a", "sv")
    store.put(a, responses={"probe": {"a": 1}}, updated=None, retrieved_at=STAMP)
    store.save()
    later = _store(tmp_path)
    later.put(
        source_key("trafa", "b", "sv"),
        responses={"probe": {"b": 1}},
        updated="2026",
        retrieved_at=STAMP,
    )
    later.save()
    reopened = _store(tmp_path)
    assert set(reopened.entries()) == {a, "trafa/b/sv"}
    assert reopened.get_shared("trafa", "products.json") == [{"id": 1}]
    assert reopened.get_shared("trafa", "documentation.T1.html") == "<p>x</p>"
    assert (tmp_path / "source/trafa/_shared/documentation.T1.html").read_text(
        encoding="utf-8"
    ) == "<p>x</p>"
    assert reopened.manifest["shared"] == {
        "trafa/_shared/documentation.T1.html": {"retrieved_at": STAMP},
        "trafa/_shared/products.json": {"retrieved_at": STAMP},
    }
    assert reopened.get_shared("trafa", "absent.json") is None


def test_save_deletes_files_of_removed_entries_only(tmp_path):
    store = _store(tmp_path)
    a, b = (source_key("trafa", c, "sv") for c in "ab")
    store.put(
        a,
        responses={"probe": {"a": 1}},
        updated=None,
        retrieved_at=STAMP,
        files={"labels.csv": "x;y\n"},
    )
    store.put(b, responses={"probe": {"b": 1}}, updated=None, retrieved_at=STAMP)
    store.save()
    assert store.entries()[a]["files"] == ["trafa/a.sv.json", "trafa/a.sv.labels.csv"]
    stray = tmp_path / "source/trafa/stray.json"
    stray.write_text("{}", encoding="utf-8")
    store = _store(tmp_path)
    store.remove(a)
    store.save()
    remaining = sorted(
        p.relative_to(tmp_path / "source").as_posix()
        for p in (tmp_path / "source").rglob("*")
        if p.is_file()
    )
    assert remaining == ["manifest.json", "trafa/b.sv.json"]
    assert _store(tmp_path).entries() == {
        b: {"files": ["trafa/b.sv.json"], "updated": None, "retrieved_at": STAMP}
    }


def test_updated_is_stored_verbatim(tmp_path):
    store = _store(tmp_path)
    store.put(
        source_key("p", "n", "sv"),
        responses={"cube": {"rows": [0, False]}},
        updated=1730000000000,
        retrieved_at=STAMP,
    )
    store.put(
        source_key("p", "m", "sv"),
        responses={"cube": {"rows": []}},
        updated=None,
        retrieved_at=STAMP,
        derived={"coverage": {"years": []}},
    )
    store.save()
    raw = json.loads((tmp_path / "source/manifest.json").read_text(encoding="utf-8"))
    assert raw["adapter"] == "trafa" and raw["version"] == 1
    assert raw["entries"]["p/n/sv"]["updated"] == 1730000000000
    assert raw["entries"]["p/m/sv"]["updated"] is None
    assert _store(tmp_path).get("p/n/sv")["responses"] == {"cube": {"rows": [0, False]}}
    assert _store(tmp_path).get("p/m/sv")["derived"] == {"coverage": {"years": []}}


def test_harvest_manifest_excludes_metadata(tmp_path):
    (tmp_path / "metadata").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "metadata/x.jsonl.gz").write_bytes(b"1")
    (tmp_path / "state/current.jsonl.gz").write_bytes(b"2")
    (tmp_path / "result.json").write_bytes(b"3")
    assert list(harvest_manifest(tmp_path)) == ["state/current.jsonl.gz"]


def test_unchanged_put_keeps_retrieved_at_and_bytes(tmp_path):
    store = _store(tmp_path)
    key = source_key("trafa", "t1", "sv")
    store.put(key, responses={"probe": {"a": 1}}, updated=None, retrieved_at=STAMP)
    store.save()
    before = (tmp_path / "source/manifest.json").read_bytes()
    again = _store(tmp_path)
    again.put(key, responses={"probe": {"a": 1}}, updated=None, retrieved_at="later")
    again.save()
    assert (tmp_path / "source/manifest.json").read_bytes() == before
    assert again.get(key)["retrieved_at"] == STAMP
    again.put(key, responses={"probe": {"a": 2}}, updated=None, retrieved_at="later")
    again.save()
    assert again.get(key)["retrieved_at"] == "later"
    assert again.entries()[key]["retrieved_at"] == "later"


def test_put_normalises_tuples_to_json_lists(tmp_path):
    store = _store(tmp_path)
    key = source_key("smhi", "x", "sv")
    store.put(
        key,
        responses={"probe": {"years": (2020, 2021)}},
        derived={"listed_in": ("1.1", "2.3")},
        updated=None,
        retrieved_at=STAMP,
    )
    store.save()
    envelope = _store(tmp_path).get(key)
    assert envelope["responses"] == {"probe": {"years": [2020, 2021]}}
    assert envelope["derived"] == {"listed_in": ["1.1", "2.3"]}
    again = _store(tmp_path)
    again.put(
        key,
        responses={"probe": {"years": (2020, 2021)}},
        derived={"listed_in": ("1.1", "2.3")},
        updated=None,
        retrieved_at="later",
    )
    assert again.get(key)["retrieved_at"] == STAMP

import asyncio
import itertools
import json
import math
from pathlib import Path

import aiohttp
import pytest

from harvesting.adapters.sdb import retrieval
from harvesting.adapters.sdb.retrieval import (
    chunk_codes,
    parse_value,
    retrieve,
    to_https,
)

SDB_DATA = json.loads(
    (Path(__file__).parent / "fixtures" / "sdb.json").read_text(encoding="utf-8")
)
AMNING = {
    name: [str(category["id"]) for category in categories]
    for name, categories in SDB_DATA["categories"]["sv"]["amning"].items()
}
ROW_KEYS = {
    "region": "regionId",
    "alder": "alderId",
    "matt": "mattId",
    "ar": "ar",
    "variabel": "variabelId",
}


def amning_rows() -> tuple[list[dict], dict[tuple, tuple]]:
    """Generate result rows with absent, flagged and decimal-comma cells."""
    rows, expected = [], {}
    names = list(ROW_KEYS)
    for number, codes in enumerate(itertools.product(*(AMNING[n] for n in names))):
        cell = dict(zip(names, codes, strict=True))
        if number % 7 == 3:
            continue
        if number % 11 == 5:
            varde, value, flag = "..", None, ".."
        elif cell["matt"] == "2":
            varde, value, flag = f"{number},5", number + 0.5, None
        else:
            varde, value, flag = str(number), number, None
        row = {
            ROW_KEYS[name]: code if name == "variabel" else int(code)
            for name, code in cell.items()
        }
        rows.append({**row, "varde": varde})
        expected[tuple(cell[name] for name in names)] = (value, flag)
    return rows, expected


@pytest.fixture
def amning(sdb):
    rows, expected = amning_rows()
    sdb.fake.rows["amning"] = rows
    return expected


def fetch(api_root, selection, *, subject="amning", data_url=True, metadata_url=True):
    async def main():
        async with aiohttp.ClientSession() as session:
            fragment = await retrieve(
                session,
                provider_code="socialstyrelsen",
                dataset_code=subject,
                language="sv",
                selection=selection,
                data_url=f"{api_root}/sv/{subject}/resultat" if data_url else None,
                metadata_url=f"{api_root}/sv/{subject}" if metadata_url else None,
                metadata={},
            )
            assert not session.closed
            return fragment

    return asyncio.run(main())


def expected_sparse(fragment, expected):
    """Return the sparse value and status maps the fake rows should produce."""
    names = list(ROW_KEYS)
    indices = [
        fragment["dimension"][code]["category"]["index"] for code in fragment["id"]
    ]
    values, flags = {}, {}
    for cell, codes in enumerate(itertools.product(*indices)):
        coordinate = dict(zip(fragment["id"], codes, strict=True))
        key = tuple(coordinate[name] for name in names)
        if key in expected:
            value, flag = expected[key]
            values[str(cell)] = value
            if flag is not None:
                flags[str(cell)] = flag
    return values, flags


SELECTION = {
    "variabel": ["T", "E"],
    "region": ["1", "0"],
    "alder": ["2"],
    "matt": ["2", "1"],
    "ar": AMNING["ar"],
}


def test_retrieve_returns_selection_in_selection_order(sdb, amning, check_fragment):
    fragment = fetch(sdb.api_root, SELECTION)

    check_fragment(fragment, SELECTION)
    assert fragment["id"] == list(SELECTION)
    for code, codes in SELECTION.items():
        assert fragment["dimension"][code]["category"]["index"] == codes
    values, flags = expected_sparse(fragment, amning)
    assert fragment["value"] == values
    assert fragment.get("status", {}) == flags


def test_retrieve_returns_only_cells_sdb_returned(sdb, amning, check_fragment):
    selection = {name: AMNING[name] for name in ROW_KEYS}

    fragment = fetch(sdb.api_root, selection)

    check_fragment(fragment, selection)
    assert len(fragment["value"]) == len(amning) < math.prod(fragment["size"])
    assert set(fragment["status"].values()) == {".."}
    assert all(fragment["value"][cell] is None for cell in fragment["status"])
    assert any(isinstance(value, float) for value in fragment["value"].values())
    assert 0 in fragment["value"].values()


def test_retrieve_splits_long_code_lists(sdb, amning, monkeypatch, check_fragment):
    unchunked = fetch(sdb.api_root, SELECTION)
    monkeypatch.setattr(retrieval, "MAX_SEGMENT_LENGTH", 4)
    sdb.fake.requests.clear()

    chunked = fetch(sdb.api_root, SELECTION)

    check_fragment(chunked, SELECTION)
    assert chunked == unchunked
    assert len(sdb.fake.requests) == len(AMNING["ar"])  # one request per year
    for request in sdb.fake.requests:
        segments = request.split("?")[0].split("/resultat/")[1].split("/")[1::2]
        assert all(len(segment) <= 4 for segment in segments)


def test_retrieve_follows_pagination(sdb, amning, monkeypatch):
    unpaged = fetch(sdb.api_root, SELECTION)
    monkeypatch.setattr(retrieval, "PAGE_SIZE", 2)
    sdb.fake.requests.clear()

    paged = fetch(sdb.api_root, SELECTION)

    assert paged == unpaged
    assert any("sida=2" in request for request in sdb.fake.requests)


def test_retrieve_returns_no_cells_when_sdb_has_no_rows(sdb, amning):
    sdb.fake.rows["amning"] = [
        row for row in sdb.fake.rows["amning"] if row["regionId"] != 3
    ]

    fragment = fetch(sdb.api_root, {**SELECTION, "region": ["3"]})

    assert fragment["value"] == {}
    assert "status" not in fragment


def test_retrieve_raises_lookup_error_for_removed_subject(sdb, amning):
    with pytest.raises(LookupError, match="finnsinte"):
        fetch(sdb.api_root, SELECTION, subject="finnsinte")


def test_retrieve_builds_sdb_urls_when_harvested_urls_are_missing(
    sdb, amning, monkeypatch
):
    expected = fetch(sdb.api_root, SELECTION)
    monkeypatch.setattr(retrieval, "API_ROOT", sdb.api_root)

    assert (
        fetch(sdb.api_root, SELECTION, data_url=False, metadata_url=False) == expected
    )


def test_retrieve_rejects_omitted_dimension(sdb, amning):
    selection = {name: codes for name, codes in SELECTION.items() if name != "variabel"}

    with pytest.raises(ValueError, match="variabel"):
        fetch(sdb.api_root, selection)


@pytest.mark.parametrize("selection", [{}, {**SELECTION, "alder": []}])
def test_retrieve_rejects_empty_selections(sdb, selection):
    with pytest.raises(ValueError):
        fetch(sdb.api_root, selection)


def test_retrieve_rejects_conflicting_duplicate_rows(sdb, amning):
    duplicate = next(row for row in sdb.fake.rows["amning"] if row["varde"] == "0")
    sdb.fake.rows["amning"].append({**duplicate, "varde": "99"})
    selection = {name: AMNING[name] for name in ROW_KEYS}

    with pytest.raises(RuntimeError, match="conflicting"):
        fetch(sdb.api_root, selection)


def test_retrieve_propagates_server_errors(sdb, amning):
    sdb.fake.failing_paths.add("/api/v1/sv/amning/resultat")

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        fetch(sdb.api_root, SELECTION)
    assert caught.value.status == 500


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("19116", (19116, None)),
        ("0", (0, None)),
        ("12,5", (12.5, None)),
        ("-3.25", (-3.25, None)),
        ("1 234", (1234, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("..", (None, "..")),
        (7, (7, None)),
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == expected


def test_chunk_codes_respects_segment_length():
    codes = [f"A{number:02}" for number in range(10)]

    chunks = chunk_codes(codes, 11)

    assert [code for chunk in chunks for code in chunk] == codes
    assert all(len(",".join(chunk)) <= 11 for chunk in chunks)
    assert chunk_codes(["TOOLONGCODE"], 3) == [["TOOLONGCODE"]]


def test_to_https_only_rewrites_sdb_links():
    assert (
        to_https("http://sdb.socialstyrelsen.se/api/v1/sv/amning/resultat/")
        == "https://sdb.socialstyrelsen.se/api/v1/sv/amning/resultat"
    )
    assert to_https("http://127.0.0.1:8080/api/v1") == "http://127.0.0.1:8080/api/v1"


def test_retrieve_uses_harvested_result_field_names(sdb, amning):
    expected = fetch(sdb.api_root, SELECTION)
    sdb.fake.result_names["amning"] = {"variabel": "grupp"}
    sdb.fake.rows["amning"] = [
        {
            ("gruppId" if key == "variabelId" else key): value
            for key, value in row.items()
        }
        for row in sdb.fake.rows["amning"]
    ]
    sdb.fake.requests.clear()

    async def main():
        async with aiohttp.ClientSession() as session:
            return await retrieve(
                session,
                provider_code="socialstyrelsen",
                dataset_code="amning",
                language="sv",
                selection=SELECTION,
                data_url=f"{sdb.api_root}/sv/amning/resultat",
                metadata_url=f"{sdb.api_root}/sv/amning",
                metadata={retrieval.RESULT_DIMENSIONS_POINTER: {"variabel": "grupp"}},
            )

    assert asyncio.run(main()) == expected
    assert all("/grupp/" in request for request in sdb.fake.requests)

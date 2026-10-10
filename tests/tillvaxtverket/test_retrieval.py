"""Retrieval against an imitation of DivePort built from recorded rows."""

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest

from retrieval.adapters import tillvaxtverket as retrieval
from tests.tillvaxtverket.conftest import NAME, metadata_for

TYPES = ["Camping", "Hotell", "SoL", "Stugbyar", "Vandrarhem"]


def run(retrieve_from, fake, selection, **kwargs):
    return asyncio.run(retrieve_from(fake, selection, **kwargs))


def as_map(value):
    return (
        {str(i): v for i, v in enumerate(value)} if isinstance(value, list) else value
    )


def test_values_are_numbers_in_selection_order(fake, retrieve_from, check_fragment):
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": TYPES,
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER", "LOGIINTAKT"],
    }

    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    # Recorded as floats (1.6737801E7, 2.896119327E9, ...) in integer columns.
    assert fragment["value"] == [
        16737801,
        2896119327,
        41905476,
        32503438936,
        4933484,
        1651343684,
        2564102,
        977097138,
        2908706,
        1132215438,
    ]
    assert all(type(v) is int for v in fragment["value"])
    assert "status" not in fragment


def test_exact_request_shape(fake, retrieve_from):
    selection = {
        "Region": ["lan:03", "lan:01"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2023", "2024"],
        "ContentsCode": ["LOGIINTAKT"],
    }

    run(retrieve_from, fake, selection)

    assert fake.requests == [
        [
            ("format", "json"),
            ("filter:NIVA_NAMN", "Län"),
            ("filter:LAN_KOD", "03"),
            ("filter:LAN_KOD", "01"),
            ("filter:ANLAGGNINGSTYP_NAMN", "Hotell"),
            ("filter:AR", "2023"),
            ("filter:AR", "2024"),
            ("column", "NIVA_NAMN"),
            ("column", "LAN_KOD"),
            ("column", "KOMMUN_KOD"),
            ("column", "ANLAGGNINGSTYP_NAMN"),
            ("column", "AR"),
            ("column", "LOGIINTAKT"),
            ("limit", "10000"),
            ("offset", "0"),
        ]
    ]


def test_recorded_suppressed_county_and_municipality_filter_independently(
    fake, retrieve_from, check_fragment
):
    """Recorded region identities, with explicitly synthetic measure values."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/suppressed_county.json").read_text(
            encoding="utf-8"
        )
    )
    template = {**fake.rows[0], "AR": 2024, "ANLAGGNINGSTYP_NAMN": "Hotell"}
    county = {**template, **fixture["rows"][0], "ANTAL_GASTNATTER": 17}
    municipality = {
        **template,
        "NIVA_NAMN": "Kommun",
        "LAN_KOD": "01",
        "KOMMUN_KOD": "Sekretesskyddad",
        "ANTAL_GASTNATTER": 23,
    }
    fake.rows = [county, municipality]
    selection = {
        "Region": ["lan:Sekretesskyddad", "kommun:01:Sekretesskyddad"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }

    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    assert fragment["value"] == [17, 23]
    requests = {dict(pairs)["filter:NIVA_NAMN"]: pairs for pairs in fake.requests}
    assert ("filter:LAN_KOD", "Sekretesskyddad") in requests["Län"]
    assert ("filter:LAN_KOD", "01") in requests["Kommun"]
    assert ("filter:LAN_KOD", "Sekretesskyddad") not in requests["Kommun"]


def test_zero_is_kept(fake, retrieve_from, check_fragment):
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": ["Camping"],
        "Tid": ["2024"],
        "ContentsCode": ["DISPONIBLA_BADDAR", "ANTAL_ANLAGGNINGAR"],
    }

    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    assert fragment["value"] == [0, 1034]


def test_text_marker_is_null_with_status_and_missing_cells_are_left_out(
    fake, retrieve_from, check_fragment
):
    selection = {
        "Region": ["kommun:01:Sekretesskyddad", "kommun:0117"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER", "LOGIINTAKT"],
    }

    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    # (residual, ANTAL), (residual, LOGI) marker, (0117, ANTAL); (0117, LOGI) absent.
    assert fragment["value"] == {"0": 12345, "1": None, "2": 54321}
    assert fragment["status"] == {"1": "Sekretesskyddad"}
    # Only municipality rows, narrowed to KOMMUN_KOD 0117 or the merged rows.
    filters = [p for p in fake.requests[0] if p[0].startswith("filter:")]
    assert ("filter:NIVA_NAMN", "Kommun") in filters
    assert ("filter:KOMMUN_KOD", "Sekretesskyddad") in filters
    assert not any(name == "filter:LAN_KOD" for name, _ in filters)


def test_only_merged_rows_narrow_to_their_counties(fake, retrieve_from):
    selection = {
        "Region": ["kommun:01:Sekretesskyddad"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }

    fragment = run(retrieve_from, fake, selection)

    assert fragment["value"] == [12345]
    assert ("filter:LAN_KOD", "01") in fake.requests[0]


def test_levels_are_separate_requests_and_absent_rows_sparse(
    fake, retrieve_from, check_fragment
):
    selection = {
        "Region": ["riket", "lan:01", "kommun:0180"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2023", "2024"],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }

    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    levels = [dict(r)["filter:NIVA_NAMN"] for r in fake.requests]
    assert levels == ["Kommun", "Län", "Riket"]

    # Recorded rows: Riket 2024, Län 01 2023 and 2024, Stockholm 2024.
    def guests(level, county, municipality, year):
        row = next(
            r
            for r in fake.rows
            if (r["NIVA_NAMN"], r["LAN_KOD"], r["KOMMUN_KOD"])
            == (level, county, municipality)
            and r["ANLAGGNINGSTYP_NAMN"] == "Hotell"
            and r["AR"] == year
        )
        return int(row["ANTAL_GASTNATTER"])

    assert fragment["value"] == {
        "1": guests("Riket", "Sekretesskyddad", "Sekretesskyddad", 2024),
        "2": guests("Län", "01", "Sekretesskyddad", 2023),
        "3": guests("Län", "01", "Sekretesskyddad", 2024),
        "5": guests("Kommun", "01", "0180", 2024),
    }
    assert fragment["value"]["1"] == 41905476


def test_paging_follows_row_count(fake, retrieve_from, monkeypatch):
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": TYPES,
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }
    whole = run(retrieve_from, fake, selection)
    fake.requests.clear()
    monkeypatch.setattr(retrieval, "PAGE_SIZE", 2)

    fragment = run(retrieve_from, fake, selection)

    assert [dict(r)["offset"] for r in fake.requests] == ["0", "2", "4"]
    assert fragment == whole


@pytest.mark.parametrize("failure", ["empty", "changed", "missing", "negative"])
def test_untrustworthy_pagination_raises_instead_of_returning_partial(
    fake, retrieve_from, monkeypatch, failure
):
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": TYPES,
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }
    get = retrieval._get

    async def damaged(session, url, params):
        body = await get(session, url, params)
        if dict(params)["offset"] != "0":
            if failure == "empty":
                body["rows"] = []
            elif failure == "changed":
                body["rowCount"] += 1
            elif failure == "missing":
                body.pop("rowCount")
            else:
                body["rowCount"] = -1
        return body

    monkeypatch.setattr(retrieval, "PAGE_SIZE", 2)
    monkeypatch.setattr(retrieval, "_get", damaged)
    with pytest.raises(RuntimeError, match="pagination|rowCount"):
        run(retrieve_from, fake, selection)


def test_request_cap_covers_short_pages(fake, retrieve_from, monkeypatch):
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": TYPES,
        "Tid": ["2024"],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }
    get = retrieval._get

    async def short_page(session, url, params):
        body = await get(session, url, params)
        body["rows"] = body["rows"][:1]
        return body

    monkeypatch.setattr(retrieval, "PAGE_SIZE", 2)
    monkeypatch.setattr(retrieval, "MAX_REQUESTS", 3)
    monkeypatch.setattr(retrieval, "_get", short_page)
    with pytest.raises(ValueError, match="requests"):
        run(retrieve_from, fake, selection)
    assert len(fake.requests) == 3


def test_long_selections_are_split(fake, retrieve_from, check_fragment, monkeypatch):
    selection = {
        "Region": ["lan:01", "lan:03"],
        "Anlaggningstyp": TYPES,
        "Tid": ["2023", "2024"],
        "ContentsCode": ["ANTAL_GASTNATTER", "BELAGDA_RUM"],
    }
    whole = run(retrieve_from, fake, selection)
    limit = len(fake.urls[0]) - 40
    fake.requests.clear()
    fake.urls.clear()
    monkeypatch.setattr(retrieval, "MAX_URL_LENGTH", limit)

    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    assert len(fake.requests) > 1
    assert all(len(url) <= limit for url in fake.urls)
    assert fragment == whole


@pytest.mark.parametrize(
    ("selection", "patch", "message"),
    [
        (
            {"Region": ["riket"], "Tid": ["2024"], "ContentsCode": ["LOGIINTAKT"]},
            {},
            r"missing \['Anlaggningstyp'\]",
        ),
        (
            {
                "Region": ["riket"],
                "Anlaggningstyp": ["Hotell"],
                "Tid": ["2024"],
                "ContentsCode": [],
            },
            {},
            "measures",
        ),
        (
            {
                "Region": ["socken:1"],
                "Anlaggningstyp": ["Hotell"],
                "Tid": ["2024"],
                "ContentsCode": ["LOGIINTAKT"],
            },
            {},
            "unknown Region code",
        ),
        (
            {
                "Region": ["riket"],
                "Anlaggningstyp": ["Hotell"],
                "Tid": ["2024"],
                "ContentsCode": ["LOGIINTAKT"],
            },
            {"MAX_URL_LENGTH": 50},
            "cannot be split",
        ),
        (
            {
                "Region": ["riket", "lan:01"],
                "Anlaggningstyp": TYPES,
                "Tid": ["2023", "2024"],
                "ContentsCode": ["LOGIINTAKT"],
            },
            {"PAGE_SIZE": 1, "MAX_REQUESTS": 3},
            "requests",
        ),
    ],
    ids=["missing dimension", "no measure", "bad region", "unsplittable", "cap"],
)
def test_invalid_selections_are_value_errors(
    fake, retrieve_from, monkeypatch, selection, patch, message
):
    for name, value in patch.items():
        monkeypatch.setattr(retrieval, name, value)

    with pytest.raises(ValueError, match=message):
        run(retrieve_from, fake, selection)
    assert fake.requests == []


def test_request_cap_is_checked_against_row_count(fake, retrieve_from, monkeypatch):
    monkeypatch.setattr(retrieval, "PAGE_SIZE", 1)
    monkeypatch.setattr(retrieval, "MAX_REQUESTS", 6)
    selection = {
        "Region": ["lan:01", "lan:03"],
        "Anlaggningstyp": ["Hotell", "Camping"],
        "Tid": ["2024"],
        "ContentsCode": ["LOGIINTAKT"],
    }
    row = next(
        r
        for r in fake.rows
        if r["NIVA_NAMN"] == "Län" and r["AR"] == 2024 and r["LAN_KOD"] == "01"
    )
    fake.rows += [dict(row) for _ in range(5)]  # more rows than the selection spans

    with pytest.raises(ValueError, match="requests"):
        run(retrieve_from, fake, selection, metadata=metadata_for())
    assert len(fake.requests) == 1


def test_removed_column_is_a_value_error(fake, retrieve_from):
    fake.columns.remove("LOGIINTAKT")
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["LOGIINTAKT"],
    }

    with pytest.raises(ValueError, match="not found"):
        run(retrieve_from, fake, selection)


def test_missing_dataset_is_a_lookup_error(fake, retrieve_from):
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["LOGIINTAKT"],
    }

    with pytest.raises(LookupError, match="not found"):
        run(retrieve_from, fake, selection, name="GuestNights_Gone")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_upstream_failures_propagate(fake, retrieve_from, status):
    fake.fail_status = status
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["LOGIINTAKT"],
    }

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        run(retrieve_from, fake, selection)
    assert caught.value.status == status


def test_conflicting_rows_raise(fake, retrieve_from):
    fake.rows.append(dict(fake.rows[0], ANTAL_GASTNATTER=1))
    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": [fake.rows[0]["ANLAGGNINGSTYP_NAMN"]],
        "Tid": [str(fake.rows[0]["AR"])],
        "ContentsCode": ["ANTAL_GASTNATTER"],
    }

    with pytest.raises(RuntimeError, match="conflicting"):
        run(retrieve_from, fake, selection)


def test_caller_session_stays_open_and_reusable(fake):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    selection = {
        "Region": ["riket"],
        "Anlaggningstyp": ["Hotell"],
        "Tid": ["2024"],
        "ContentsCode": ["LOGIINTAKT"],
    }

    async def scenario():
        app = web.Application()
        app.router.add_get("/api/api/query/inkvartering/data/{name}", fake.handle)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "tillvaxtverket",
                "dataset_code": f"inkvartering/{NAME}",
                "language": "sv",
                "selection": selection,
                "data_url": str(
                    server.make_url(f"/api/api/query/inkvartering/data/{NAME}.cbase")
                )
                + "?format=json",
                "metadata_url": None,
                "metadata": metadata_for(),
            }
            first = await retrieval.retrieve(session, **arguments)
            gone = str(server.make_url("/api/api/query/inkvartering/data/X.cbase"))
            with pytest.raises(LookupError):
                await retrieval.retrieve(session, **{**arguments, "data_url": gone})
            second = await retrieval.retrieve(session, **arguments)
            return first, second, session.closed

    first, second, closed = asyncio.run(scenario())

    assert (
        first
        == second
        == {
            "id": ["Region", "Anlaggningstyp", "Tid", "ContentsCode"],
            "size": [1, 1, 1, 1],
            "dimension": {
                name: {"category": {"index": codes}}
                for name, codes in selection.items()
            },
            "value": [32503438936],
        }
    )
    assert closed is False


def test_region_codes_round_trip():
    assert retrieval.region_code("Riket", "Riket", "Riket") == "riket"
    assert retrieval.region_code("Riket", "Sekretesskyddad", "x") == "riket"
    assert retrieval.region_code("Län", "01", "Sekretesskyddad") == "lan:01"
    assert retrieval.region_code("Kommun", "01", "0180") == "kommun:0180"
    assert retrieval.region_filter("kommun:01:Sekretesskyddad") == (
        "Kommun",
        "KOMMUN_KOD",
        "Sekretesskyddad",
    )
    with pytest.raises(ValueError):
        retrieval.region_filter("lan:01:x")

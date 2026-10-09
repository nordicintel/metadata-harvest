"""Retrieval against an imitation of SMHI's getdata built from recorded responses."""

import asyncio

import aiohttp
import pytest

from metadata_harvest.adapters.smhi_emissions import retrieval
from tests.smhi_emissions.conftest import load

YEARS = ["1990", "2000", "2005", "2010", "2015", "2020", "2021", "2022", "2023", "2024"]


def run(retrieve_from, fake, selection, **kwargs):
    return asyncio.run(retrieve_from(fake, selection, **kwargs))


def points(name, substance, label):
    body = load(name)
    return dict(
        (p["year"], p["value"]) for p in next(iter(body["data"].values()))[label]
    )


def as_map(value):
    return (
        {str(i): v for i, v in enumerate(value)} if isinstance(value, list) else value
    )


def selection(region, sectors, years=YEARS, substance="CO2"):
    return {
        "Region": region,
        "Sektor": sectors,
        "Tid": years,
        "ContentsCode": [substance],
    }


def test_national_total_and_main_sectors_with_zeros(
    fake_smhi, retrieve_from, check_fragment
):
    chosen = selection(["00"], ["0", "4", "9"])

    fragment = run(retrieve_from, fake_smhi, chosen)

    check_fragment(fragment, chosen)
    assert isinstance(fragment["value"], list)  # every cell provided: dense
    national = "getdata-national-CO2.json"
    total = points(national, "CO2", "Totalt")
    foreign = points(national, "CO2", "Utrikes transporter")
    assert fragment["value"][:10] == [total[y] for y in YEARS]
    assert fragment["value"][20] == 0.0  # Utrikes transporter 1990: kept as 0.0
    assert fragment["value"][29] == foreign["2024"]
    assert fake_smhi.queries == [
        {
            "county": "0",
            "municipality": "0",
            "hsektor": "0",
            "usektor": "0",
            "sub": "CO2",
        }
    ]


def test_subsectors_need_their_sector_request(fake_smhi, retrieve_from, check_fragment):
    chosen = selection(["00"], ["4.1", "0", "4"], ["2024"])

    fragment = run(retrieve_from, fake_smhi, chosen)

    check_fragment(fragment, chosen)
    cars = points("getdata-national-CO2-transporter.json", "CO2", "Personbilar")
    national = "getdata-national-CO2.json"
    assert fragment["value"] == [
        cars["2024"],
        points(national, "CO2", "Totalt")["2024"],
        # The sector itself comes from hsektor=0, not the sector request's Totalt.
        points(national, "CO2", "Transporter")["2024"],
    ]
    assert [q["hsektor"] for q in fake_smhi.queries] == ["0", "4"]


def test_one_request_per_region_with_leading_zeros_removed(
    fake_smhi, retrieve_from, check_fragment
):
    chosen = selection(["01", "00"], ["0"], ["2024"])

    fragment = run(retrieve_from, fake_smhi, chosen)

    check_fragment(fragment, chosen)
    assert fragment["value"] == [
        points("getdata-stockholms-lan-CO2.json", "CO2", "Totalt")["2024"],
        points("getdata-national-CO2.json", "CO2", "Totalt")["2024"],
    ]
    assert [(q["county"], q["municipality"]) for q in fake_smhi.queries] == [
        ("1", "0"),
        ("0", "0"),
    ]


def test_kommun_years_missing_from_a_series_are_left_out(
    fake_smhi, retrieve_from, check_fragment
):
    """Recorded: Kiruna's "Odling av mineraljordar" has no 1990 and no 2024 point."""
    chosen = selection(["2584"], ["7.11", "7.1"], ["1990", "2024"], substance="CH4")

    fragment = run(retrieve_from, fake_smhi, chosen, substance="CH4")

    check_fragment(fragment, chosen)
    body = "getdata-kiruna-CH4-jordbruk.json"
    digestion = points(body, "CH4", "Djurs matsmältning")
    assert fragment["value"] == {"2": digestion["1990"], "3": digestion["2024"]}
    assert fake_smhi.queries == [
        {
            "county": "25",
            "municipality": "2584",
            "hsektor": "7",
            "usektor": "0",
            "sub": "CH4",
        }
    ]


def test_capitalisation_drift_in_labels_is_tolerated(
    fake_smhi, retrieve_from, check_fragment
):
    """Recorded (submission 2025): "inrikes civil sjöfart" in lower case."""
    chosen = selection(["0180"], ["4.9"], ["2023"], substance="NOx")

    fragment = run(retrieve_from, fake_smhi, chosen, substance="NOx")

    check_fragment(fragment, chosen)
    assert fragment["value"] == [165.54945190423405]


def test_selection_order_defines_fragment_order(
    fake_smhi, retrieve_from, check_fragment
):
    chosen = {
        "Tid": ["2024", "1990"],
        "ContentsCode": ["CO2"],
        "Sektor": ["4", "0"],
        "Region": ["00"],
    }

    fragment = run(
        retrieve_from,
        fake_smhi,
        chosen,
        metadata={
            "/id": ["Tid", "ContentsCode", "Sektor", "Region"],
            "/extension/smhi_emissions": _native(),
        },
    )

    check_fragment(fragment, chosen)
    assert fragment["id"] == ["Tid", "ContentsCode", "Sektor", "Region"]
    national = "getdata-national-CO2.json"
    assert fragment["value"][0] == points(national, "CO2", "Transporter")["2024"]
    assert fragment["value"][3] == points(national, "CO2", "Totalt")["1990"]


def test_no_data_returns_empty_value(fake_smhi, retrieve_from, check_fragment):
    """An unrecorded region answers with no series, as SMHI does for county=99."""
    assert load("getdata-unknown-county-CO2.json")["data"] == {"Koldioxid (CO2)": {}}
    chosen = selection(["0114"], ["0"], ["2024"])

    fragment = run(retrieve_from, fake_smhi, chosen)

    check_fragment(fragment, chosen)
    assert fragment["value"] == {}


def test_unknown_sector_label_is_an_adapter_error(fake_smhi, retrieve_from):
    """Synthetic: the response names a sector the harvested tree lacks."""
    body = fake_smhi.data[("0", "0", "0", "CO2")]
    body["data"]["Koldioxid (CO2)"]["Rymdfart"] = []

    with pytest.raises(RuntimeError, match="Rymdfart"):
        run(retrieve_from, fake_smhi, selection(["00"], ["0"], ["2024"]))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_source_value_is_rejected(value):
    body = {"data": {"CO2": {"Totalt": [{"year": 2024, "value": value}]}}}
    with pytest.raises(RuntimeError, match="unexpected point"):
        retrieval.series(body, "national CO2")


def test_request_cap_is_a_value_error(fake_smhi, retrieve_from, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_REQUESTS", 3)

    with pytest.raises(ValueError, match="needs 4 requests"):
        run(retrieve_from, fake_smhi, selection(["00", "01"], ["0", "4.1"], ["2024"]))
    assert fake_smhi.queries == []


@pytest.mark.parametrize(
    ("chosen", "message"),
    [
        ({"Region": ["00"], "Tid": ["2024"], "ContentsCode": ["CO2"]}, "missing"),
        (selection(["00"], ["0"], ["2024"], substance="NOx"), "must select 'CO2'"),
        (selection(["00"], ["4.99"], ["2024"]), "unknown sector codes"),
        (selection(["123"], ["0"], ["2024"]), "not an SCB region code"),
    ],
    ids=["missing dimension", "other substance", "unknown sector", "bad region"],
)
def test_invalid_selections(fake_smhi, retrieve_from, chosen, message):
    with pytest.raises(ValueError, match=message):
        run(retrieve_from, fake_smhi, chosen)
    assert fake_smhi.queries == []


def test_gone_substance_is_a_lookup_error(fake_smhi, retrieve_from):
    """SMHI answers an unknown ``sub`` with Django's 500 page."""
    with pytest.raises(LookupError, match="XYZ"):
        run(
            retrieve_from,
            fake_smhi,
            selection(["00"], ["0"], ["2024"], substance="XYZ"),
            substance="XYZ",
        )
    assert fake_smhi.listings == 1


def test_server_error_for_a_listed_substance_propagates(fake_smhi, retrieve_from):
    """Synthetic: getfilteroptions lists XYZ, yet getdata answers 500."""
    fake_smhi.tree[0]["subsectors"][0]["substances"].append(
        {"XYZ": {"substance_id": 1, "long_name": "X"}}
    )
    with pytest.raises(aiohttp.ClientResponseError) as caught:
        run(
            retrieve_from,
            fake_smhi,
            selection(["00"], ["0"], ["2024"], substance="XYZ"),
            substance="XYZ",
        )
    assert caught.value.status == 500


@pytest.mark.parametrize("status", [500, 503])
def test_upstream_failures_propagate(fake_smhi, retrieve_from, status):
    fake_smhi.fail_status = status

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        run(retrieve_from, fake_smhi, selection(["00"], ["0"], ["2024"]))

    assert caught.value.status == status


def test_metadata_without_sector_tree_is_rejected(fake_smhi, retrieve_from):
    with pytest.raises(ValueError, match="sector tree"):
        run(
            retrieve_from,
            fake_smhi,
            selection(["00"], ["0"], ["2024"]),
            metadata={},
        )


def test_caller_session_stays_open_and_reusable(fake_smhi):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def scenario():
        app = web.Application()
        app.router.add_get("/api/getdata/", fake_smhi.getdata)
        app.router.add_get("/api/getfilteroptions/", fake_smhi.getfilteroptions)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "smhi",
                "dataset_code": "emissionsdatabasen_CO2",
                "language": "sv",
                "selection": selection(["00"], ["0"], ["2024"]),
                "data_url": str(server.make_url("/api/getdata/")),
                "metadata_url": str(server.make_url("/api/getfilteroptions/")),
                "metadata": {"/extension/smhi_emissions": _native()},
            }
            first = await retrieval.retrieve(session, **arguments)
            with pytest.raises(LookupError):
                await retrieval.retrieve(
                    session,
                    **{
                        **arguments,
                        "dataset_code": "emissionsdatabasen_XYZ",
                        "metadata": {
                            "/extension/smhi_emissions": {
                                **_native(),
                                "substance": {"code": "XYZ"},
                            }
                        },
                        "selection": selection(["00"], ["0"], ["2024"], "XYZ"),
                    },
                )
            second = await retrieval.retrieve(session, **arguments)
            return first, second, session.closed

    first, second, closed = asyncio.run(scenario())

    assert first == second
    assert closed is False


def _native():
    from tests.smhi_emissions.conftest import native

    return native("CO2")

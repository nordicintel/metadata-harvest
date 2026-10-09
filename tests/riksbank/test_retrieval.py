"""Retrieval against an in-process imitation of the Riksbank APIs."""

import asyncio

import aiohttp
import pytest

from metadata_harvest.adapters.riksbank import retrieval

DAYS = ["2026-10-03", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
"""2026-10-03 is a Saturday: no observation exists."""


def run(retrieve_from, fake, api, selection, **overrides):
    return asyncio.run(retrieve_from(fake, api, selection, **overrides))


def as_map(value):
    return (
        {str(i): v for i, v in enumerate(value)} if isinstance(value, list) else value
    )


def test_swea_values_are_sparse_and_exact(fake_riksbank, retrieve_from, check_fragment):
    selection = {"Series": ["SECBREPOEFF", "SECBDEPOEFF"], "Time": DAYS}

    fragment = run(retrieve_from, fake_riksbank, "swea", selection)

    check_fragment(fragment, selection)
    assert as_map(fragment["value"]) == {
        "1": 1.75,
        "2": 1.75,
        "3": 1.75,
        "4": 1.75,
        "6": 1.65,
        "7": 1.65,
        "8": 1.65,
        "9": 1.65,
    }
    assert "status" not in fragment
    assert fake_riksbank.requests == [
        "/swea/v1/Observations/SECBREPOEFF/2026-10-03/2026-10-08",
        "/swea/v1/Observations/SECBDEPOEFF/2026-10-03/2026-10-08",
    ]
    assert fake_riksbank.accept == ["application/json"] * 2


def test_complete_selection_is_dense(fake_riksbank, retrieve_from, check_fragment):
    selection = {"Time": ["2026-10-08", "2026-10-05"], "Series": ["SECBLENDEFF"]}

    fragment = run(retrieve_from, fake_riksbank, "swea", selection)

    check_fragment(fragment, selection)
    assert fragment["id"] == ["Time", "Series"]
    assert fragment["value"] == [1.85, 1.85]


def test_series_without_data_in_range_is_checked_not_failed(
    fake_riksbank, retrieve_from, check_fragment
):
    selection = {"Series": ["SECBMARGEFF", "SECBREPOEFF"], "Time": DAYS}

    fragment = run(retrieve_from, fake_riksbank, "swea", selection)

    check_fragment(fragment, selection)
    assert set(as_map(fragment["value"])) == {"6", "7", "8", "9"}  # repo rate only
    assert fake_riksbank.requests[-1] == "/swea/v1/Series/ByGroup/2"


def test_swea_range_is_split(fake_riksbank, retrieve_from, check_fragment, monkeypatch):
    selection = {
        "Series": ["SEKEURPMI"],
        "Time": ["2026-09-28", "2026-10-02", "2026-10-08"],
    }
    whole = run(retrieve_from, fake_riksbank, "swea", selection)
    fake_riksbank.requests.clear()
    monkeypatch.setattr(retrieval, "MAX_SPAN_DAYS", 5)

    fragment = run(retrieve_from, fake_riksbank, "swea", selection)

    check_fragment(fragment, selection)
    assert fake_riksbank.requests == [
        "/swea/v1/Observations/SEKEURPMI/2026-09-28/2026-10-02",
        "/swea/v1/Observations/SEKEURPMI/2026-10-08/2026-10-08",
    ]
    assert fragment == whole
    assert fragment["value"] == [11.3205, 11.29, 11.194]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({}, r"SWEA series \['NOSUCH'\]"),
        ({"metadata_url": "/swea/v1/Series/ByGroup/9999"}, "group"),
    ],
    ids=["series gone", "group gone"],
)
def test_swea_gone_is_lookup_error(fake_riksbank, retrieve_from, overrides, message):
    selection = {"Series": ["NOSUCH"], "Time": DAYS}
    with pytest.raises(LookupError, match=message):
        run(retrieve_from, fake_riksbank, "swea", selection, **overrides)


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ({"Series": ["SECBREPOEFF"]}, r"missing \['Time'\]"),
        ({"Series": ["SECBREPOEFF"], "Time": ["2026-10"]}, "ISO dates"),
        ({"Series": ["SECBREPOEFF"], "Time": DAYS, "Region": ["00"]}, "unknown"),
    ],
    ids=["missing dimension", "non-date period", "unknown dimension"],
)
def test_invalid_selections_are_value_errors(
    fake_riksbank, retrieve_from, selection, message
):
    with pytest.raises(ValueError, match=message):
        run(retrieve_from, fake_riksbank, "swea", selection)
    assert fake_riksbank.requests == []


def test_forecast_rounds_and_missing_vintage(
    fake_riksbank, retrieve_from, check_fragment
):
    selection = {
        "Series": ["SEQGDPNAASA"],
        "PolicyRound": ["2026:3", "2026:1", "2026:2"],
        "Time": ["1981-03-31", "2029-06-30", "2029-09-30"],
    }

    fragment = run(retrieve_from, fake_riksbank, "mpd", selection)

    check_fragment(fragment, selection)
    # 2026:1 has no vintage in the table (404): its cells are left out.
    assert as_map(fragment["value"]) == {
        "0": 667938,
        "1": 1795031,
        "2": 1801211,
        "6": 668125,
        "7": 1786244,
    }
    assert fake_riksbank.requests == [
        "/monetary_policy_data/v1/forecasts?series=SEQGDPNAASA&policy_round_name=2026:3",
        "/monetary_policy_data/v1/forecasts?series=SEQGDPNAASA&policy_round_name=2026:1",
        "/monetary_policy_data/v1/forecasts?series=SEQGDPNAASA&policy_round_name=2026:2",
    ]


def test_forecast_series_gone_is_lookup_error(fake_riksbank, retrieve_from):
    selection = {
        "Series": ["SEQNOSUCH"],
        "PolicyRound": ["2026:3"],
        "Time": ["2029-09-30"],
    }
    with pytest.raises(LookupError, match="SEQNOSUCH"):
        run(
            retrieve_from,
            fake_riksbank,
            "mpd",
            selection,
            data_url="/monetary_policy_data/v1/forecasts?series=SEQNOSUCH",
        )
    assert fake_riksbank.requests[-1] == "/monetary_policy_data/v1/forecasts/series_ids"


def test_swestr_values_and_status(fake_riksbank, retrieve_from, check_fragment):
    selection = {
        "Measure": ["rate", "numberOfAgents"],
        "Time": ["2026-10-01", "2026-10-03", "2026-10-05"],
    }

    fragment = run(retrieve_from, fake_riksbank, "swestr", selection)

    check_fragment(fragment, selection)
    # numberOfAgents is null on 2026-10-05: left out, not reported as zero.
    assert as_map(fragment["value"]) == {"0": 1.647, "2": 1.638, "3": 6}
    assert fragment["status"] == {"2": "republication; alternative calculation: 1"}
    assert fake_riksbank.requests == [
        "/swestr/v1/SWESTR?fromDate=2026-10-01&toDate=2026-10-05"
    ]


def test_swestr_unknown_id_is_lookup_error(fake_riksbank, retrieve_from):
    with pytest.raises(LookupError):
        run(
            retrieve_from,
            fake_riksbank,
            "swestr",
            {"Measure": ["rate"], "Time": ["2026-10-01"]},
            data_url="/swestr/v1/NOSUCH",
        )


def test_swestr_unknown_measure_is_value_error(fake_riksbank, retrieve_from):
    with pytest.raises(ValueError, match="measures"):
        run(
            retrieve_from,
            fake_riksbank,
            "swestr",
            {"Measure": ["spread"], "Time": ["2026-10-01"]},
        )


def test_no_data_returns_empty_value(fake_riksbank, retrieve_from, check_fragment):
    selection = {"Measure": ["rate"], "Time": ["2026-10-03"]}

    fragment = run(retrieve_from, fake_riksbank, "swestr", selection)

    check_fragment(fragment, selection)
    assert fragment["value"] == {}


@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.parametrize("api", ["swea", "mpd", "swestr"])
def test_upstream_failures_propagate(fake_riksbank, retrieve_from, status, api):
    fake_riksbank.fail_status = status
    selection = {
        "swea": {"Series": ["SECBREPOEFF"], "Time": DAYS},
        "mpd": {
            "Series": ["SEQGDPNAASA"],
            "PolicyRound": ["2026:3"],
            "Time": ["2029-09-30"],
        },
        "swestr": {"Measure": ["rate"], "Time": ["2026-10-01"]},
    }[api]

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        run(retrieve_from, fake_riksbank, api, selection)

    assert caught.value.status == status
    assert len(fake_riksbank.requests) == 1  # never retried


def test_dataset_code_identifies_the_api(fake_riksbank, retrieve_from):
    fragment = run(
        retrieve_from,
        fake_riksbank,
        "swea",
        {"Series": ["SECBREPOEFF"], "Time": ["2026-10-05"]},
        metadata={},
    )
    assert fragment["value"] == [1.75]
    with pytest.raises(ValueError, match="not a Riksbank dataset"):
        run(
            retrieve_from,
            fake_riksbank,
            "swea",
            {"Series": ["SECBREPOEFF"], "Time": ["2026-10-05"]},
            dataset_code="other",
            metadata={},
        )


def test_caller_session_stays_open_and_reusable(fake_riksbank):
    from aiohttp.test_utils import TestServer

    async def scenario():
        async with (
            TestServer(fake_riksbank.app()) as server,
            aiohttp.ClientSession() as session,
        ):
            arguments = {
                "provider_code": "riksbank",
                "dataset_code": "swea_group_2",
                "language": "en",
                "selection": {"Series": ["SECBREPOEFF"], "Time": ["2026-10-05"]},
                "data_url": str(server.make_url("/swea/v1/Observations")),
                "metadata_url": str(server.make_url("/swea/v1/Series/ByGroup/2")),
                "metadata": {},
            }
            first = await retrieval.retrieve(session, **arguments)
            with pytest.raises(LookupError):
                await retrieval.retrieve(
                    session,
                    **{
                        **arguments,
                        "selection": {"Series": ["NOSUCH"], "Time": ["2026-10-05"]},
                    },
                )
            second = await retrieval.retrieve(session, **arguments)
            return first, second, session.closed

    first, second, closed = asyncio.run(scenario())

    assert (
        first
        == second
        == {
            "id": ["Series", "Time"],
            "size": [1, 1],
            "dimension": {
                "Series": {"category": {"index": ["SECBREPOEFF"]}},
                "Time": {"category": {"index": ["2026-10-05"]}},
            },
            "value": [1.75],
        }
    )
    assert closed is False


def test_date_ranges_cover_selected_dates_only():
    ranges = retrieval.date_ranges(
        ["2026-01-10", "2026-01-01", "2026-01-03", "2026-02-01"], max_days=5
    )
    assert [(a.isoformat(), b.isoformat()) for a, b in ranges] == [
        ("2026-01-01", "2026-01-03"),
        ("2026-01-10", "2026-01-10"),
        ("2026-02-01", "2026-02-01"),
    ]


@pytest.mark.parametrize(
    "vintages", [None, "broken", [None], [{"observations": "broken"}]]
)
def test_malformed_forecast_response_never_returns_empty_success(monkeypatch, vintages):
    async def malformed_get(*args, **kwargs):
        return {"data": [{"external_id": "SEQGDPNAASA", "vintages": vintages}]}

    monkeypatch.setattr(retrieval, "_get", malformed_get)
    with pytest.raises(RuntimeError, match="forecast"):
        asyncio.run(
            retrieval.retrieve(
                None,
                provider_code="riksbank",
                dataset_code="mpd_SEQGDPNAASA",
                language="en",
                selection={
                    "Series": ["SEQGDPNAASA"],
                    "PolicyRound": ["2026:3"],
                    "Time": ["2029-09-30"],
                },
                data_url=None,
                metadata_url=None,
                metadata={},
            )
        )

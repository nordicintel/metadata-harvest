"""Riksbank listings and documents built from recorded responses."""

import copy

import pytest

from apps.harvest.metadata_adapters.riksbank import mapping, source
from schema.validation import validate_metadata
from tests.support.riksbank import API, RECORDED, fixture_key

GROUPS = RECORDED[fixture_key(f"{API}/swea/v1/Groups")]
SERIES = RECORDED[fixture_key(f"{API}/swea/v1/Series")]
MPD = f"{API}/monetary_policy_data/v1/forecasts"
SERIES_IDS = RECORDED[fixture_key(f"{MPD}/series_ids")]
ROUNDS = RECORDED[fixture_key(f"{MPD}/policy_rounds")]


def latest(group_id):
    return copy.deepcopy(
        RECORDED[fixture_key(f"{API}/swea/v1/Observations/Latest/ByGroup/{group_id}")]
    )


def swea_record(code):
    listing = source.swea_listing(copy.deepcopy(GROUPS), copy.deepcopy(SERIES))
    return next(r for r in listing.records if r["dataset_code"] == code)


def test_swea_listing_has_one_record_per_group_with_series():
    listing = source.swea_listing(GROUPS, SERIES)

    assert listing.complete and not listing.problems
    assert [r["dataset_code"] for r in listing.records] == [
        "swea_group_2",
        "swea_group_130",
        "swea_group_151",
        "swea_group_155",
    ]
    group = swea_record("swea_group_2")["group"]
    assert [node["id"] for node in group["chain"]] == ["1", "137", "2"]


def test_swea_group_document():
    document = mapping.build_swea_group(swea_record("swea_group_2"), latest(2))

    validate_metadata(document, adapter="riksbank")
    series = document["dimension"]["Series"]
    assert document["id"] == ["Series", "Time"]
    assert series["category"]["index"] == [
        "SECBREPOEFF",
        "SECBDEPOEFF",
        "SECBLENDEFF",
        "SECBLIKVEFF",
        "SECBMARGEFF",
    ]
    assert series["category"]["label"]["SECBREPOEFF"] == "Policy rate"
    assert series["category"]["unit"]["SECBREPOEFF"] == {"label": "per cent"}
    assert series["category"]["note"]["SECBREPOEFF"][0].endswith("until June 8, 2022.")
    native = series["extension"]["riksbank"]["categories"]
    assert native["SECBMARGEFF"] == {
        "observationMinDate": "1987-01-30",
        "observationMaxDate": "1994-05-31",
        "seriesClosed": True,
        "shortDescription": "Marginal rate",
        "latestObservationDate": "1994-05-31",
    }
    time = document["dimension"]["Time"]["category"]["index"]
    assert time[0] == "1987-01-30" and time[-1] == "2026-10-09"
    assert document["size"] == [5, len(time)] and len(time) == len(set(time))
    shared = document["extension"]["nordicintel"]
    assert shared["discontinued"] is False
    assert (shared["first_period"], shared["last_period"]) == (
        "1987-01-30",
        "2026-10-09",
    )
    assert shared["subject"] == {"code": "137", "label": "Riksbank interest rates"}
    assert shared["metadata_url"] == f"{API}/swea/v1/Series/ByGroup/2"
    assert shared["data_url"] == f"{API}/swea/v1/Observations"
    assert document["extension"]["riksbank"]["api"] == "swea"
    assert document["extension"]["riksbank"]["group_id"] == 2


def test_currency_unit_and_unmapped_forward_premium_unit():
    currencies = mapping.build_swea_group(swea_record("swea_group_130"), latest(130))
    premiums = mapping.build_swea_group(swea_record("swea_group_155"), latest(155))

    units = currencies["dimension"]["Series"]["category"]["unit"]
    assert len(units) == 4
    assert all(unit == {"label": "SEK per unit of currency"} for unit in units.values())
    assert "unit" not in premiums["dimension"]["Series"]["category"]


def test_long_mid_description_becomes_a_note():
    record = swea_record("swea_group_2")
    row = record["series"][0]
    row["midDescription"] = (
        "STIBOR T/N (Tomorrow/Next). As of May 2020, the calculation and publishing "
        "of STIBOR is handled by the Swedish Financial Benchmark Facility (SFBF)."
    )
    row["shortDescription"] = "STIBOR T/N"

    document = mapping.build_swea_group(record, latest(2))

    category = document["dimension"]["Series"]["category"]
    assert category["label"]["SECBREPOEFF"] == "STIBOR T/N"
    assert category["note"]["SECBREPOEFF"][0] == row["midDescription"]


def test_latest_observation_for_unlisted_series_is_rejected():
    rows = latest(2) + [{"seriesId": "NOSUCH", "date": "2026-10-09", "value": 1}]
    with pytest.raises(ValueError, match="unlisted"):
        mapping.build_swea_group(swea_record("swea_group_2"), rows)


@pytest.mark.parametrize(
    ("first", "last", "frequency", "expected"),
    [
        (
            "1981-03-31",
            "1981-12-31",
            "Q",
            ["1981-03-31", "1981-06-30", "1981-09-30", "1981-12-31"],
        ),
        ("2024-01-31", "2024-03-31", "M", ["2024-01-31", "2024-02-29", "2024-03-31"]),
        ("1994-12-31", "1996-12-31", "A", ["1994-12-31", "1995-12-31", "1996-12-31"]),
        ("1981-02-15", "1981-04-01", "Q", ["1981-03-31", "1981-06-30"]),
    ],
)
def test_period_ends(first, last, frequency, expected):
    assert mapping.period_ends(first, last, frequency) == expected


def mpd_record(series_id):
    listing = source.mpd_listing(copy.deepcopy(SERIES_IDS), copy.deepcopy(ROUNDS))
    return next(r for r in listing.records if r["series"]["series_id"] == series_id)


def test_forecast_document():
    probe = RECORDED[
        fixture_key(MPD, {"series": "SEQGDPNAYSA", "policy_round_name": "2026:3"})
    ]
    document = mapping.build_mpd(mpd_record("SEQGDPNAYSA"), copy.deepcopy(probe))

    validate_metadata(document, adapter="riksbank")
    assert document["id"] == ["Series", "PolicyRound", "Time"]
    rounds = document["dimension"]["PolicyRound"]["category"]["index"]
    assert rounds[0] == "2015:1" and rounds[-1] == "2026:3" and len(rounds) == 61
    periods = document["dimension"]["Time"]["category"]["index"]
    assert periods[0] == "1981-03-31" and periods[-1] == "2029-09-30"
    assert document["size"] == [1, 61, len(periods)]
    assert document["dimension"]["Series"]["category"]["unit"]["SEQGDPNAYSA"] == {
        "label": "Annual percentage change, constant prices",
        "decimals": 2,
    }
    assert document["updated"] == "2026-09-24"
    shared = document["extension"]["nordicintel"]
    assert shared["time_unit"] == "quarterly"
    assert shared["data_url"] == f"{MPD}?series=SEQGDPNAYSA"
    native = document["extension"]["riksbank"]
    assert native["latest_policy_round"] == "2026:3"
    assert native["series_id"] == "SEQGDPNAYSA"


def test_forecast_without_latest_vintage_is_rejected():
    with pytest.raises(ValueError, match="no 2026:3 vintage"):
        mapping.build_mpd(mpd_record("SEQGDPNAYSA"), {"data": []})


def test_swestr_document():
    body = RECORDED[fixture_key(f"{API}/swestr/v1/latest/SWESTR")]
    document = mapping.build_swestr(copy.deepcopy(body))

    validate_metadata(document, adapter="riksbank")
    measures = document["dimension"]["Measure"]["category"]
    assert measures["index"][0] == "rate"
    assert measures["unit"]["volume"] == {"label": "SEK million"}
    time = document["dimension"]["Time"]["category"]["index"]
    assert time[0] == "2021-09-01" and time[-1] == body["date"]
    assert document["updated"] == "2026-10-09"


def test_listing_rows_are_isolated():
    rows = [{"seriesId": "BROKEN"}] + copy.deepcopy(SERIES)
    rows.append({**copy.deepcopy(SERIES[0]), "groupId": 130})  # duplicate id

    listing = source.swea_listing(GROUPS, rows)

    assert not listing.complete
    assert listing.quarantined == {"swea_group_2", "swea_group_130"}
    assert [r["dataset_code"] for r in listing.records] == [
        "swea_group_151",
        "swea_group_155",
    ]
    assert [resource for resource, _ in listing.problems] == [
        "swea/series.0",
        "en/swea_group_2",
        "en/swea_group_130",
    ]


def test_duplicate_forecast_series_is_quarantined():
    body = copy.deepcopy(SERIES_IDS)
    body["data"].append(copy.deepcopy(body["data"][0]))

    listing = source.mpd_listing(body, ROUNDS)

    assert listing.quarantined == {"mpd_SEQGDPNAYSA"}
    assert "mpd_SEQGDPNAYSA" not in {r["dataset_code"] for r in listing.records}


@pytest.mark.parametrize("body", [{"data": []}, {"data": ["2026"]}, [], None])
def test_malformed_policy_rounds_fail_the_scope(body):
    with pytest.raises(ValueError):
        source.policy_rounds(body)


@pytest.mark.parametrize(
    ("periods", "series_id", "expected"),
    [
        (["1994-12-31", "1995-03-31", "2029-12-31"], "SEDMENAYNA", "Q"),
        (["2026-01-31", "2026-02-28"], "SEMCPIFNAYNA", "M"),
        (["1995-12-31", "1997-12-31"], "SEAWAGNAYCA", "A"),
        (["2029-09-30"], "SEQGDPNAYSA", "Q"),
    ],
)
def test_forecast_frequency_is_inferred_from_periods(periods, series_id, expected):
    assert mapping.infer_frequency(periods, series_id) == expected


@pytest.mark.parametrize(
    "periods", [["2026-01-15", "2026-02-15"], ["2026-02-28", "2026-05-31"]]
)
def test_irregular_forecast_periods_are_rejected(periods):
    with pytest.raises(ValueError):
        mapping.infer_frequency(periods, "SEDMENAYNA")

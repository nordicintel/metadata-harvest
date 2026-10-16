"""Live retrieval against recorded UKÄ exports: fragment shape, values and errors."""

import gzip
import math
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import aiohttp
import pytest

from retrieval.data_adapters.uka import export_request, retrieve
from schema.validation import validate_retrieval
from tests.support.uka import (
    FIXTURES,
    UKA,
    FakeUka,
    RoutedSession,
    run_with_uka,
)


@pytest.fixture
def fake() -> FakeUka:
    return FakeUka()


BLEKINGE = "Blekinge tekniska högskola"


def data_url(indicator: int) -> str:
    return f"{UKA}/api/totals/{indicator}?export=all&export_type=csv"


def check_fragment(
    fragment: Mapping[str, Any], selection: Mapping[str, Sequence[str]]
) -> None:
    """Schema validation plus the RETRIEVAL.md consumer checks."""
    validate_retrieval(fragment, selection=selection)
    ids = list(selection)
    assert fragment["id"] == ids
    assert list(fragment["dimension"]) == ids
    indices = []
    for name in ids:
        index = fragment["dimension"][name]["category"]["index"]
        if isinstance(index, dict):
            assert sorted(index.values()) == list(range(len(index)))
            index = sorted(index, key=index.__getitem__)
        assert index == list(selection[name])
        indices.append(index)
    assert fragment["size"] == [len(index) for index in indices]
    total = math.prod(fragment["size"])
    for key in ("value", "status"):
        content = fragment.get(key)
        if isinstance(content, list):
            assert len(content) == total
        elif isinstance(content, dict):
            assert all(int(position) < total for position in content)
    if isinstance(fragment.get("status"), dict):
        assert isinstance(fragment["value"], dict | list)
        for position in fragment["status"]:
            value = fragment["value"]
            cell = value[int(position)] if isinstance(value, list) else value[position]
            assert cell is None


def fetch(
    fake: FakeUka,
    indicator: int,
    time: str,
    selection: Mapping[str, Sequence[str]],
    *,
    url: str | None = "default",
) -> tuple[dict[str, Any], RoutedSession]:
    """Retrieve ``selection`` of ``indicator``, check the fragment, return it."""

    async def run(session: RoutedSession):
        fragment = await retrieve(
            session,  # type: ignore[arg-type]
            provider_code="uka",
            dataset_code=str(indicator),
            language="sv",
            selection=selection,
            data_url=data_url(indicator) if url == "default" else url,
            metadata_url=f"{UKA}/api/indicators/{indicator}",
            metadata={"/role": {"time": [time]}},
        )
        return fragment, session

    fragment, session = run_with_uka(fake, run)
    check_fragment(fragment, selection)
    return fragment, session


def fails(
    fake: FakeUka,
    indicator: int,
    selection: Mapping[str, Sequence[str]],
    metadata: Mapping[str, Any],
    error: type[BaseException],
) -> pytest.ExceptionInfo:
    async def run(session: RoutedSession):
        with pytest.raises(error) as info:
            await retrieve(
                session,  # type: ignore[arg-type]
                provider_code="uka",
                dataset_code=str(indicator),
                language="sv",
                selection=selection,
                data_url=data_url(indicator),
                metadata_url=None,
                metadata=metadata,
            )
        return info

    return run_with_uka(fake, run)


def test_dense_values_follow_selection_order(fake):
    fragment, _ = fetch(
        fake,
        7,
        "Läsår",
        {
            "Läsår": ["2024/25"],
            "Lärosäte": [BLEKINGE],
            "Kön": ["Kvinnor", "Män", "Total"],
            "Åldersgrupp": ["Total"],
        },
    )
    assert fragment["value"] == [45.0, 43.4, 44.1]
    assert "status" not in fragment


def test_reversed_selection_order_moves_positions(fake):
    fragment, _ = fetch(
        fake,
        7,
        "Läsår",
        {
            "Åldersgrupp": ["Total"],
            "Kön": ["Total", "Män", "Kvinnor"],
            "Lärosäte": [BLEKINGE],
            "Läsår": ["2024/25"],
        },
    )
    assert fragment["id"] == ["Åldersgrupp", "Kön", "Lärosäte", "Läsår"]
    assert fragment["value"] == [44.1, 43.4, 45.0]


def test_last_dimension_varies_fastest(fake):
    fragment, _ = fetch(
        fake,
        7,
        "Läsår",
        {
            "Kön": ["Kvinnor", "Män"],
            "Läsår": ["2017/18", "2024/25"],
            "Lärosäte": [BLEKINGE],
            "Åldersgrupp": ["Total"],
        },
    )
    # Kvinnor 2017/18, Kvinnor 2024/25, Män 2017/18, Män 2024/25; 2017/18 has a
    # plain "Andel" measure column, 2024/25 a quoted "AndelUbak" with " 2024/25".
    assert fragment["value"] == [30.2, 45.0, 23.3, 43.4]


def test_marker_cells_are_null_with_status(fake):
    fragment, _ = fetch(
        fake,
        7,
        "Läsår",
        {
            "Läsår": ["2024/25"],
            "Lärosäte": ["Akademi för Ledarskap och Teologi"],
            "Kön": ["Kvinnor", "Män", "Total"],
            "Åldersgrupp": ["Total"],
        },
    )
    assert fragment["value"] == [None, None, 11.7]
    assert fragment["status"] == {"0": "..", "1": ".."}


def test_zero_is_kept(fake):
    fragment, _ = fetch(
        fake,
        72,
        "Läsår",
        {
            "Läsår": ["2020/21"],
            "Lärosäte": ["Ersta Sköndal Bräcke högskola"],
            "Kön": ["Kvinnor", "Män"],
            "Åldersgrupp": ["Total"],
        },
    )
    assert fragment["value"] == [1.8, 0.0]
    assert fragment["value"][1] is not None
    assert all(isinstance(value, float) for value in fragment["value"])


def test_integer_counts_stay_integers(fake):
    fragment, _ = fetch(
        fake,
        97,
        "Termin",
        {
            "Termin": ["VT2026"],
            "Lärosäte": [BLEKINGE],
            "Program": ["Sjuksköterskeexamen"],
            "Kön": ["Total"],
            "Åldersgrupp": ["Total"],
        },
    )
    assert fragment["value"] == [122]
    assert type(fragment["value"][0]) is int


def test_absent_cells_give_sparse_value(fake):
    fragment, _ = fetch(
        fake,
        97,
        "Termin",
        {
            "Termin": ["VT2026"],
            "Lärosäte": [BLEKINGE],
            "Program": ["Sjuksköterskeexamen"],
            "Kön": ["Kvinnor", "Män"],
            "Åldersgrupp": ["-24", "35-"],
        },
    )
    # Blekinge / Sjuksköterskeexamen / Män / 35- has no row.
    assert fragment["value"] == {"0": 51, "1": 22, "2": 11}
    assert "status" not in fragment


def test_hierarchy_codes_and_eliminated_dimensions(fake):
    fragment, _ = fetch(
        fake,
        15,
        "År",
        {
            "År": ["2025"],
            "Examen": ["Lärarexamina > Lärarexamina, fritidshem", "Lärarexamina"],
            "Kön": ["Kvinnor", "Män", "Total"],
        },
    )
    assert fragment["value"] == [4, 2, 6, 212, 49, 261]


def test_eliminated_dimensions_use_total_rows(fake):
    fragment, _ = fetch(
        fake, 72, "Läsår", {"Lärosäte": [BLEKINGE, "Total"], "Läsår": ["2020/21"]}
    )
    assert fragment["value"] == [5.3, 13.0]


def test_flat_group_dimensions(fake):
    fragment, _ = fetch(
        fake,
        14,
        "År",
        {
            "År": ["2025"],
            "Verksamhetsområde": ["Beställd utbildning"],
            "Finansiärsgrupp": ["Total", "Statliga myndigheter", "Företag i Sverige"],
        },
    )
    assert fragment["value"] == [157341, 74353, 179]


def test_no_data_gives_empty_value(fake):
    fragment, _ = fetch(
        fake,
        14,
        "År",
        {
            "År": ["2025"],
            "Lärosäte": ["Total"],
            "Verksamhetsområde": ["Beställd utbildning"],
            "Finansiärsgrupp": ["EU", "Anslagsmedel"],
        },
    )
    assert fragment["value"] == {}
    assert fragment["size"] == [1, 1, 1, 2]
    assert "status" not in fragment


def test_multiple_periods_one_link_and_file_each(fake):
    periods = ["2024/25", "2002/03", "2012/13"]
    fragment, session = fetch(
        fake,
        72,
        "Läsår",
        {"Läsår": periods, "Lärosäte": [BLEKINGE], "Kön": ["Kvinnor", "Män"]},
    )
    assert fragment["value"] == [2.2, 5.6, 8.0, 8.5, 8.6, 11.1]
    links = [key for key in fake.requests if key.startswith("/api/totals/72?")]
    files = [key for key in fake.requests if key.startswith("/uploads/")]
    assert sorted(links) == sorted(
        f"/api/totals/72?export=all&export_type=csv&year={p.replace('/', '%2F')}"
        for p in periods
    )
    assert len(files) == len(set(files)) == len(periods)
    assert len(fake.requests) == 2 * len(periods)
    # Every request went through the caller's session.
    assert len(session.urls) == len(fake.requests)


def test_no_other_session_is_created(fake, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("retrieve created its own session")

    async def run(session: RoutedSession):
        monkeypatch.setattr(aiohttp, "ClientSession", refuse)
        return await retrieve(
            session,  # type: ignore[arg-type]
            provider_code="uka",
            dataset_code="72",
            language="sv",
            selection={"Läsår": ["2020/21"], "Lärosäte": [BLEKINGE]},
            data_url=data_url(72),
            metadata_url=None,
            metadata={"/role": {"time": ["Läsår"]}},
        )

    assert run_with_uka(fake, run)["value"] == [5.3]


def test_missing_data_url_uses_default_api(fake):
    fragment, session = fetch(
        fake, 72, "Läsår", {"Läsår": ["2020/21"], "Lärosäte": ["Total"]}, url=None
    )
    assert fragment["value"] == [13.0]
    assert session.urls[0] == f"{UKA}/api/totals/72"
    assert (
        fake.requests[0] == "/api/totals/72?export=all&export_type=csv&year=2020%2F21"
    )


def test_unknown_period_raises_value_error(fake):
    info = fails(
        fake,
        97,
        {"Termin": ["XX1999"], "Lärosäte": ["Total"]},
        {"/role": {"time": ["Termin"]}},
        ValueError,
    )
    assert "XX1999" in str(info.value)


def test_missing_indicator_raises_lookup_error(fake):
    fake.overrides["/api/totals/72"] = (404, "Not Found")
    fails(
        fake,
        72,
        {"Läsår": ["2020/21"]},
        {"/role": {"time": ["Läsår"]}},
        LookupError,
    )


def test_indicator_without_datasets_raises_lookup_error(fake):
    fake.overrides["/api/totals/72"] = (200, {"error": "Indicator has no datasets."})
    info = fails(
        fake,
        72,
        {"Läsår": ["2020/21"]},
        {"/role": {"time": ["Läsår"]}},
        LookupError,
    )
    assert not isinstance(info.value, ValueError)


@pytest.mark.parametrize(
    ("selection", "metadata"),
    [
        ({"Läsår": ["2020/21"]}, {}),
        ({"Läsår": ["2020/21"]}, {"/role": {}}),
        ({"Lärosäte": ["Total"]}, {"/role": {"time": ["Läsår"]}}),
    ],
)
def test_missing_time_dimension_raises_value_error(fake, selection, metadata):
    fails(fake, 72, selection, metadata, ValueError)
    assert fake.requests == []


def test_server_error_propagates_without_retry(fake):
    fake.failures["/api/totals/72"] = 1
    info = fails(
        fake,
        72,
        {"Läsår": ["2020/21"]},
        {"/role": {"time": ["Läsår"]}},
        aiohttp.ClientResponseError,
    )
    assert info.value.status == 500
    assert len(fake.requests) == 1


def test_period_column_is_identified_by_position(fake):
    """Some exports misspell the period header (``Lasar``); it is not compared."""
    selection = {"År": ["2020/21"], "Lärosäte": ["Total"]}
    fragment, _ = fetch(fake, 72, "År", selection)
    expected, _ = fetch(
        fake, 72, "Läsår", {"Läsår": ["2020/21"], "Lärosäte": ["Total"]}
    )

    assert fragment["value"] == expected["value"]
    assert fragment["value"]


def test_conflicting_rows_raise_runtime_error(fake):
    path = next(p for p in fake.files if "2020-21" in p and "utlandsstudier" in p)
    content = (FIXTURES / "exports" / fake.files[path]["file"]).read_bytes()
    lines = gzip.decompress(content).decode("utf-8-sig").splitlines()
    total = next(
        line for line in lines[1:] if line.split(";")[1:4] == ["", "Total", "Total"]
    )
    duplicate = total.rsplit(";", 1)[0] + ";99,9"
    fake.overrides[path] = (200, "\n".join([*lines, duplicate]))

    fails(
        fake,
        72,
        {"Läsår": ["2020/21"], "Lärosäte": ["Total"]},
        {"/role": {"time": ["Läsår"]}},
        RuntimeError,
    )


def test_unknown_dimension_raises_runtime_error(fake):
    fails(
        fake,
        72,
        {"Läsår": ["2020/21"], "Program": ["Total"]},
        {"/role": {"time": ["Läsår"]}},
        RuntimeError,
    )


def test_export_request_keeps_data_url_params():
    url, params = export_request("72", data_url(72), "2020/21")
    assert url == f"{UKA}/api/totals/72"
    assert params == {"export": "all", "export_type": "csv", "year": "2020/21"}


def test_export_request_replaces_year_in_data_url():
    url, params = export_request("72", data_url(72) + "&year=2002/03", "2020/21")
    assert url == f"{UKA}/api/totals/72"
    assert params["year"] == "2020/21"


def test_export_request_without_data_url():
    url, params = export_request("97", None, "VT2026")
    assert url == f"{UKA}/api/totals/97"
    assert params == {"export": "all", "export_type": "csv", "year": "VT2026"}


@pytest.mark.parametrize(
    "body",
    [
        "Läsår;Lärosäte;Kön;Åldersgrupp;Andel\n2020/21;;Total\n",
        "Läsår;Lärosäte[1];Kön;Åldersgrupp;Andel\n",
        "",
    ],
)
def test_malformed_export_raises_runtime_error(fake, body):
    path = next(p for p in fake.files if "2020-21" in p and "utlandsstudier" in p)
    fake.overrides[path] = (200, body)

    info = fails(
        fake,
        72,
        {"Läsår": ["2020/21"], "Lärosäte": ["Total"]},
        {"/role": {"time": ["Läsår"]}},
        RuntimeError,
    )
    assert "malformed UKÄ export" in str(info.value)
    assert not isinstance(info.value, ValueError)


def test_non_json_link_answer_raises_runtime_error(fake):
    query = {"export": "all", "export_type": "csv", "year": "2020/21"}
    fake.overrides[f"/api/totals/72?{urlencode(sorted(query.items()))}"] = (
        200,
        "<html>maintenance</html>",
    )

    fails(
        fake,
        72,
        {"Läsår": ["2020/21"], "Lärosäte": ["Total"]},
        {"/role": {"time": ["Läsår"]}},
        RuntimeError,
    )

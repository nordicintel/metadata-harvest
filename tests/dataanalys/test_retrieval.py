"""Retrieval against an imitation of Superset's /chart/data built from G082's rows."""

import asyncio
import copy
import math

import aiohttp
import pytest

from harvesting.adapters.dataanalys import retrieval
from harvesting.semantics import category_codes
from tests.dataanalys.conftest import check_fragment, retrieve_from

COLUMNS = {
    "Region": "Indelning_etikett",
    "Kon": "Kon",
    "Alder": "Alder",
    "Tid": "Matperiod_char",
    "ContentsCode": "Matt_id",
}


def full_selection(document):
    return {
        name: category_codes(document["dimension"][name]["category"])
        for name in document["id"]
    }


def expected(rows, selection):
    """Independently compute value/status maps from the recorded rows."""
    value, status = {}, {}
    ids = list(selection)
    for row in rows:
        codes = {name: row[COLUMNS[name]] for name in ids}
        if any(codes[name] not in selection[name] for name in ids):
            continue
        if row["Varde"] is None and not row["Kommentar"]:
            continue
        index = 0
        for name in ids:
            index = index * len(selection[name]) + selection[name].index(codes[name])
        value[str(index)] = row["Varde"]
        if row["Kommentar"]:
            status[str(index)] = row["Kommentar"]
    return value, status


def as_map(value):
    return (
        {str(i): v for i, v in enumerate(value)} if isinstance(value, list) else value
    )


def run(fake, document, selection, **overrides):
    return asyncio.run(retrieve_from(fake, document, selection, **overrides))


def test_whole_cube_is_dense_with_markers_and_annotated_values(fake_superset, g082):
    selection = full_selection(g082)

    fragment = run(fake_superset, g082, selection)

    check_fragment(fragment, selection)
    assert isinstance(fragment["value"], list) and len(fragment["value"]) == 264
    value, status = expected(fake_superset.rows, selection)
    assert as_map(fragment["value"]) == value
    assert fragment["status"] == status
    missing = [k for k, flag in status.items() if flag.startswith("Uppgift saknas")]
    assert len(missing) == 18 and all(value[k] is None for k in missing)
    annotated = [k for k, flag in status.items() if flag.startswith("Exkl.")]
    assert annotated and all(isinstance(value[k], float) for k in annotated)
    assert len(fake_superset.bodies) == 1


def test_exact_request_shape(fake_superset, g082):
    selection = {
        "Region": ["Riket", "Uppsala"],
        "Kon": ["Kön, totalt"],
        "Alder": ["Ålder, totalt"],
        "Tid": ["2025"],
        "ContentsCode": ["G082"],
    }

    run(fake_superset, g082, selection)

    assert fake_superset.bodies == [
        {
            "datasource": {"id": 159, "type": "table"},
            "force": False,
            "result_format": "json",
            "result_type": "full",
            "queries": [
                {
                    "columns": [
                        "Indelning_typ",
                        "Indelning_etikett",
                        "Kon",
                        "Alder",
                        "Matperiod_char",
                        "Matt_id",
                        "Varde",
                        "Kommentar",
                    ],
                    "metrics": [],
                    "filters": [
                        {
                            "col": "Indelning_typ",
                            "op": "IN",
                            "val": ["Riket", "Region"],
                        },
                        {
                            "col": "Indelning_etikett",
                            "op": "IN",
                            "val": ["Riket", "Uppsala"],
                        },
                        {"col": "Kon", "op": "IN", "val": ["Kön, totalt"]},
                        {"col": "Alder", "op": "IN", "val": ["Ålder, totalt"]},
                        {"col": "Matperiod_char", "op": "IN", "val": ["2025"]},
                        {"col": "Matt_id", "op": "IN", "val": ["G082"]},
                    ],
                    "row_limit": 3,
                    "orderby": [],
                    "extras": {"where": "", "having": ""},
                }
            ],
        }
    ]


def test_selection_order_defines_fragment_order(fake_superset, g082):
    selection = {
        "Tid": ["2025", "2022"],
        "ContentsCode": ["G082"],
        "Kon": ["Män", "Kvinnor"],
        "Alder": ["Ålder, totalt"],
        "Region": ["Stockholm"],
    }

    fragment = run(fake_superset, g082, selection)

    check_fragment(fragment, selection)
    assert fragment["id"] == list(selection)
    assert fragment["size"] == [2, 1, 2, 1, 1]
    value, status = expected(fake_superset.rows, selection)
    assert as_map(fragment["value"]) == value
    assert fragment.get("status", {}) == status
    # Position 0 is (2025, G082, Män, total, Stockholm): the last varies fastest.
    first = next(
        r
        for r in fake_superset.rows
        if (r["Matperiod_char"], r["Kon"], r["Indelning_etikett"])
        == ("2025", "Män", "Stockholm")
    )
    assert as_map(fragment["value"])["0"] == first["Varde"]


def test_absent_cells_and_empty_comments_are_left_out(fake_superset, g082):
    """Synthetic changes to recorded rows: one row removed, one null without a
    comment."""
    selection = {
        "Region": ["Riket", "Uppsala"],
        "Kon": ["Kön, totalt", "Kvinnor"],
        "Alder": ["Ålder, totalt"],
        "Tid": ["2024", "2025"],
        "ContentsCode": ["G082"],
    }
    rows = fake_superset.rows

    def match(row, region, sex, period):
        return (row["Indelning_etikett"], row["Kon"], row["Matperiod_char"]) == (
            region,
            sex,
            period,
        )

    rows.remove(next(r for r in rows if match(r, "Riket", "Kvinnor", "2025")))
    blank = next(r for r in rows if match(r, "Riket", "Kvinnor", "2024"))
    blank.update(Varde=None, Kommentar="")

    fragment = run(fake_superset, g082, selection)

    check_fragment(fragment, selection)
    assert isinstance(fragment["value"], dict)
    assert {"2", "3"}.isdisjoint(fragment["value"])
    value, status = expected(rows, selection)
    assert fragment["value"] == value
    assert fragment.get("status", {}) == status


def test_no_rows_is_an_empty_value(fake_superset, g082):
    fake_superset.rows = [
        r for r in fake_superset.rows if r["Matperiod_char"] != "2022"
    ]
    selection = full_selection(g082) | {"Tid": ["2022"]}

    fragment = run(fake_superset, g082, selection)

    check_fragment(fragment, selection)
    assert fragment["value"] == {} and "status" not in fragment


@pytest.mark.parametrize("limit", [7, 40])
def test_large_selections_are_split(fake_superset, g082, monkeypatch, limit):
    selection = full_selection(g082)
    whole = run(fake_superset, g082, selection)
    fake_superset.bodies.clear()
    monkeypatch.setattr(retrieval, "MAX_ROWS", limit)

    fragment = run(fake_superset, g082, selection)

    check_fragment(fragment, selection)
    assert len(fake_superset.bodies) > 1
    for query in fake_superset.queries:
        filters = {f["col"]: f["val"] for f in query["filters"]}
        cells = math.prod(
            len(filters[column]) for column in COLUMNS.values() if column in filters
        )
        assert cells <= limit and query["row_limit"] == cells + 1
    assert fragment == whole


def test_split_prefers_region_and_time(monkeypatch):
    selection = {
        "Kon": ["x", "y", "z"],
        "Region": ["a", "b", "c", "d"],
        "Tid": ["1", "2"],
        "ContentsCode": ["G"],
    }
    monkeypatch.setattr(retrieval, "MAX_ROWS", 6)

    parts = retrieval.split_selection(selection)

    assert [(part["Region"], part["Tid"]) for part in parts] == [
        (["a"], ["1", "2"]),
        (["b"], ["1", "2"]),
        (["c"], ["1", "2"]),
        (["d"], ["1", "2"]),
    ]
    assert all(part["Kon"] == ["x", "y", "z"] for part in parts)

    monkeypatch.setattr(retrieval, "MAX_ROWS", 0)
    with pytest.raises(ValueError, match="cannot be split"):
        retrieval.split_selection(selection)


def test_selection_missing_a_dimension_is_rejected(fake_superset, g082):
    selection = full_selection(g082)
    del selection["Kon"]

    with pytest.raises(ValueError, match=r"missing \['Kon'\]"):
        run(fake_superset, g082, selection)
    assert fake_superset.bodies == []


def test_missing_column_is_a_value_error(fake_superset, g082):
    document = copy.deepcopy(g082)
    document["extension"]["socialstyrelsen_analys"]["columns"]["Kon"] = "Gender"

    with pytest.raises(ValueError, match="lacks columns"):
        run(fake_superset, document, full_selection(g082))


def test_gone_datasource_is_a_lookup_error(fake_superset, g082):
    document = copy.deepcopy(g082)
    document["extension"]["socialstyrelsen_analys"]["values_dataset_id"] = 999999

    with pytest.raises(LookupError, match="404"):
        run(fake_superset, document, full_selection(g082))


def test_failed_query_is_an_upstream_error(fake_superset, g082):
    fake_superset.query_status = "failed"

    with pytest.raises(RuntimeError, match="Query timed out"):
        run(fake_superset, g082, full_selection(g082))


def test_conflicting_rows_are_an_error(fake_superset, g082):
    """Synthetic: a second value for one cell."""
    row = copy.deepcopy(fake_superset.rows[0])
    row["Varde"] = 99.0
    fake_superset.rows.append(row)

    with pytest.raises(RuntimeError, match="rows for 264 cells"):
        run(fake_superset, g082, full_selection(g082))


@pytest.mark.parametrize("status", [500, 503, 403])
def test_other_failures_propagate(fake_superset, g082, status):
    fake_superset.fail_status = status

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        run(fake_superset, g082, full_selection(g082))

    assert caught.value.status == status


def test_caller_session_stays_open_and_reusable(fake_superset, g082):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from tests.dataanalys.conftest import metadata_of

    async def scenario():
        app = web.Application()
        app.router.add_post("/api/v1/chart/data", fake_superset.handle)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "socialstyrelsen_analys",
                "dataset_code": "GONV|G082",
                "language": "sv",
                "selection": full_selection(g082),
                "data_url": str(server.make_url("/api/v1/chart/data")),
                "metadata_url": None,
                "metadata": metadata_of(g082),
            }
            first = await retrieval.retrieve(session, **arguments)
            gone = copy.deepcopy(arguments["metadata"])
            gone["/extension/socialstyrelsen_analys"]["values_dataset_id"] = 1
            with pytest.raises(LookupError):
                await retrieval.retrieve(session, **{**arguments, "metadata": gone})
            second = await retrieval.retrieve(session, **arguments)
            return first, second, session.closed

    first, second, closed = asyncio.run(scenario())

    assert first == second
    assert closed is False


def test_required_metadata_is_static_and_present(g082):
    assert retrieval.REQUIRED_METADATA == ("/id", "/extension/socialstyrelsen_analys")
    native = g082["extension"]["socialstyrelsen_analys"]
    assert native["values_dataset_id"] == 159
    assert set(native["columns"]) >= {*g082["id"], "value", "status"}

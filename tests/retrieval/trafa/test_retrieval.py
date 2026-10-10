"""Retrieval against an imitation of Trafikanalys' /data built from real tables."""

import asyncio
import math
import re
from urllib.parse import parse_qs

import aiohttp
import pytest

from retrieval.data_adapters import trafa as retrieval

DRIVM = "t10011|itrfslut|ar|drivm"
HIERARCHY = "t10011|avstslut|itrfslut|ar|agarkat|tillst"
DECIMAL = "t0604_rt_ar|ftagplan|rt0|ar"


def table_cells(fake, table):
    """Recorded cells of ``table`` as {(variable codes…, measure): cell}."""
    body = fake.tables[table]
    variables = [c["Name"] for c in body["Header"]["Column"] if c["Type"] == "D"]
    cells = {}
    for row in body["Rows"]:
        codes = {c["Column"]: c["Name"] for c in row["Cell"] if not c["IsMeasure"]}
        for cell in row["Cell"]:
            if cell["IsMeasure"]:
                cells[(*(codes[v] for v in variables), cell["Column"])] = cell
    return variables, cells


def codes_of(fake, table, variable):
    _, cells = table_cells(fake, table)
    variables = table_cells(fake, table)[0]
    position = variables.index(variable)
    return sorted({key[position] for key in cells})


def expected(fake, table, selection, fixed=None):
    """Independently compute the fragment's value/status maps from the table."""
    variables, cells = table_cells(fake, table)
    fixed = fixed or {}
    ids = list(selection)
    value, status = {}, {}
    notes = fake.tables[table]["Notes"]
    for key, cell in cells.items():
        by_name = dict(zip([*variables, "measure"], key, strict=True))
        if any(by_name[name] != code for name, code in fixed.items()):
            continue
        if any(by_name[name] not in selection[name] for name in ids):
            continue
        index = 0
        for name in ids:
            index = index * len(selection[name]) + selection[name].index(by_name[name])
        text = cell["Value"]
        flags = [notes[str(i)] for i in cell["NoteIds"] or []]
        if re.fullmatch(r"-?\d+(,\d+)?", text):
            value[str(index)] = (
                float(text.replace(",", ".")) if "," in text else int(text)
            )
        elif text:  # a marker such as "-" or ".."
            value[str(index)] = None
            flags.insert(0, text)
        if flags:
            status[str(index)] = "; ".join(flags)
    return value, status


def as_map(value):
    return (
        {str(i): v for i, v in enumerate(value)} if isinstance(value, list) else value
    )


def run(retrieve_from, fake, selection, **kwargs):
    return asyncio.run(retrieve_from(fake, selection, **kwargs))


def test_values_zeros_markers_and_revision_notes(
    fake_trafa, retrieve_from, check_fragment
):
    selection = {
        "ar": codes_of(fake_trafa, DRIVM, "ar"),
        "drivm": codes_of(fake_trafa, DRIVM, "drivm"),
        "measure": ["itrfslut"],
    }

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    value, status = expected(fake_trafa, DRIVM, selection)
    assert as_map(fragment["value"]) == value
    assert fragment["status"] == status
    assert list(value.values()).count(0) == 3  # provided zeros stay 0
    markers = [key for key, flag in status.items() if flag == "-"]
    assert len(markers) == 3 and all(value[key] is None for key in markers)
    revised = [key for key, flag in status.items() if flag == "Reviderad uppgift"]
    assert len(revised) == 6 and all(isinstance(value[k], int) for k in revised)
    assert fake_trafa.queries == [
        "t10011|itrfslut|ar:"
        + ",".join(selection["ar"])
        + "|drivm:"
        + ",".join(selection["drivm"])
    ]


def test_absent_cells_are_left_out(fake_trafa, retrieve_from, check_fragment):
    selection = {
        "ar": codes_of(fake_trafa, DRIVM, "ar"),
        "drivm": codes_of(fake_trafa, DRIVM, "drivm"),
        "measure": ["itrfslut"],
    }

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    total = math.prod(fragment["size"])
    assert isinstance(fragment["value"], dict)
    assert len(fragment["value"]) == 212 < total  # rows Trafikanalys returned


def test_selection_order_defines_fragment_order(
    fake_trafa, retrieve_from, check_fragment
):
    selection = {
        "measure": ["itrfslut"],
        "drivm": ["103", "101", "t1"],
        "ar": ["2024", "2010"],
    }

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    assert fragment["id"] == ["measure", "drivm", "ar"]
    assert fragment["size"] == [1, 3, 2]
    assert fragment["dimension"]["drivm"]["category"]["index"] == ["103", "101", "t1"]
    value, status = expected(fake_trafa, DRIVM, selection)
    assert as_map(fragment["value"]) == value
    assert fragment.get("status", {}) == status
    _, cells = table_cells(fake_trafa, DRIVM)
    # Position 0 is (itrfslut, 103, 2024): the last dimension varies fastest.
    assert as_map(fragment["value"])["0"] == int(
        cells[("2024", "103", "itrfslut")]["Value"]
    )


def test_complete_selection_is_dense(fake_trafa, retrieve_from, check_fragment):
    selection = {"ar": ["2024"], "drivm": ["101", "102"], "measure": ["itrfslut"]}

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    assert isinstance(fragment["value"], list) and len(fragment["value"]) == 2


def test_hierarchy_with_several_measures(fake_trafa, retrieve_from, check_fragment):
    selection = {
        "ar": ["2023", "2024"],
        "agarkat": ["10", "20", "t1"],
        "tillst": ["1", "2", "t1"],
        "measure": ["itrfslut", "avstslut"],
    }

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    value, _ = expected(fake_trafa, HIERARCHY, selection)
    assert as_map(fragment["value"]) == value
    # tillst is broken down only for agarkat 20 ("Juridisk person").
    assert len(value) == 2 * 5 * 2
    assert fake_trafa.queries == [
        "t10011|itrfslut|avstslut|ar:2023,2024|agarkat:10,20,t1|tillst:1,2,t1"
    ]


def test_eliminated_dimension_uses_the_total(fake_trafa, retrieve_from, check_fragment):
    """The service selects the harvested elimination_value "t1"."""
    selection = {
        "ar": ["2022", "2023", "2024"],
        "drivm": ["t1"],
        "measure": ["itrfslut"],
    }

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    assert fake_trafa.queries == ["t10011|itrfslut|ar:2022,2023,2024|drivm:t1"]
    value, _ = expected(fake_trafa, DRIVM, selection)
    assert as_map(fragment["value"]) == value and len(value) == 3


def test_selection_missing_a_dimension_is_rejected(fake_trafa, retrieve_from):
    metadata = {
        "/id": ["ar", "drivm", "measure"],
        "/extension/trafa": {"product": "t10011", "measure_dimension": "measure"},
    }

    with pytest.raises(ValueError, match=r"missing \['drivm'\]"):
        run(
            retrieve_from,
            fake_trafa,
            {"ar": ["2024"], "measure": ["itrfslut"]},
            metadata=metadata,
        )
    assert fake_trafa.queries == []


def test_decimal_values_and_other_markers(fake_trafa, retrieve_from, check_fragment):
    selection = {
        "ar": codes_of(fake_trafa, DECIMAL, "ar"),
        "measure": ["rt0", "ftagplan"],
    }

    fragment = run(retrieve_from, fake_trafa, selection, product="t0604_rt_ar")

    check_fragment(fragment, selection)
    value, status = expected(fake_trafa, DECIMAL, selection)
    values = as_map(fragment["value"])
    assert values == value
    assert fragment.get("status", {}) == status
    # rt0 values such as "65,9862588756494" are decimals with a comma.
    assert values[str(selection["ar"].index("2013") * 2)] == 65.9862588756494
    assert isinstance(values[str(selection["ar"].index("2013") * 2 + 1)], int)
    assert list(status.values()) == [".."]  # recorded marker other than "-"
    assert values[next(iter(status))] is None


def test_no_data_returns_empty_value(fake_trafa, retrieve_from, check_fragment):
    _, cells = table_cells(fake_trafa, DRIVM)
    missing = next(
        (year, fuel)
        for year in codes_of(fake_trafa, DRIVM, "ar")
        for fuel in codes_of(fake_trafa, DRIVM, "drivm")
        if (year, fuel, "itrfslut") not in cells
    )
    selection = {"ar": [missing[0]], "drivm": [missing[1]], "measure": ["itrfslut"]}

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    assert fragment["value"] == {}
    assert "status" not in fragment


@pytest.mark.parametrize(("limit", "rows"), [(150, None), (None, 5)])
def test_large_selections_are_split(
    fake_trafa, retrieve_from, check_fragment, monkeypatch, limit, rows
):
    selection = {
        "ar": codes_of(fake_trafa, DRIVM, "ar"),
        "drivm": codes_of(fake_trafa, DRIVM, "drivm"),
        "measure": ["itrfslut"],
    }
    whole = run(retrieve_from, fake_trafa, selection)
    fake_trafa.queries.clear()
    fake_trafa.query_strings.clear()
    if limit:
        monkeypatch.setattr(retrieval, "MAX_QUERY_STRING", limit)
    if rows:
        monkeypatch.setattr(retrieval, "MAX_ROWS", rows)

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    assert len(fake_trafa.queries) > 1
    if limit:
        assert all(len(raw) <= limit for raw in fake_trafa.query_strings)
    if rows:
        for query in fake_trafa.queries:
            parts = [p.split(":")[1].split(",") for p in query.split("|") if ":" in p]
            assert math.prod(len(p) for p in parts) <= rows
    assert as_map(fragment["value"]) == as_map(whole["value"])
    assert fragment["status"] == whole["status"]


def test_trafikanalys_query_string_limit_is_respected(
    fake_trafa, retrieve_from, check_fragment, monkeypatch
):
    """Many codes (years repeated under fake names) exceed the real 2048 limit."""
    table = fake_trafa.tables[DRIVM]
    for row in list(table["Rows"]):
        for n in range(1, 200):
            copied = {**row, "Cell": [dict(c) for c in row["Cell"]]}
            for cell in copied["Cell"]:
                if cell["Column"] == "ar":
                    cell["Name"] = f"{cell['Name']}{n:03d}"
            table["Rows"].append(copied)
        break
    years = sorted(
        {c["Name"] for r in table["Rows"] for c in r["Cell"] if c["Column"] == "ar"}
    )
    selection = {"ar": years, "drivm": ["101"], "measure": ["itrfslut"]}

    fragment = run(retrieve_from, fake_trafa, selection)

    check_fragment(fragment, selection)
    assert len(fake_trafa.queries) > 1
    assert all(len(raw) <= 2048 for raw in fake_trafa.query_strings)


def test_unsplittable_selection_is_a_value_error(
    fake_trafa, retrieve_from, monkeypatch
):
    monkeypatch.setattr(retrieval, "MAX_QUERY_STRING", 20)

    with pytest.raises(ValueError, match="exceeds"):
        run(retrieve_from, fake_trafa, {"ar": ["2024"], "measure": ["itrfslut"]})
    assert fake_trafa.queries == []


@pytest.mark.parametrize(
    ("product", "selection", "error", "message"),
    [
        ("t99999", {"ar": ["2024"], "measure": ["itrfslut"]}, LookupError, "t99999"),
        ("t10011", {"ar": ["2024"], "measure": ["nosuch"]}, LookupError, "nosuch"),
        ("t10011", {"ar": ["1800"], "measure": ["itrfslut"]}, ValueError, "parse"),
        ("t10011", {"ar": ["2024"]}, ValueError, "measures"),
    ],
    ids=["product gone", "item gone", "unknown code", "no measure"],
)
def test_errors(fake_trafa, retrieve_from, product, selection, error, message):
    with pytest.raises(error, match=message):
        run(retrieve_from, fake_trafa, selection, product=product)


@pytest.mark.parametrize("status", [500, 503])
def test_upstream_failures_propagate(fake_trafa, retrieve_from, status):
    fake_trafa.fail_status = status

    with pytest.raises(aiohttp.ClientResponseError) as caught:
        run(retrieve_from, fake_trafa, {"ar": ["2024"], "measure": ["itrfslut"]})

    assert caught.value.status == status


def test_requests_accept_json_and_use_the_document_language(fake_trafa, retrieve_from):
    run(
        retrieve_from,
        fake_trafa,
        {"ar": ["2024"], "measure": ["itrfslut"]},
        language="en",
    )

    assert fake_trafa.accept == ["application/json"]
    assert parse_qs(fake_trafa.query_strings[0])["lang"] == ["EN"]


def test_product_falls_back_to_the_dataset_code(fake_trafa, retrieve_from):
    fragment = run(
        retrieve_from,
        fake_trafa,
        {"ar": ["2024"], "measure": ["itrfslut"]},
        dataset_code="T10011|itrfslut|ar|drivm",
        metadata={},
    )

    assert fake_trafa.queries == ["t10011|itrfslut|ar:2024"]
    assert len(as_map(fragment["value"])) == 1


def test_caller_session_stays_open_and_reusable(fake_trafa):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def scenario():
        app = web.Application()
        app.router.add_get("/api/data", fake_trafa.handle)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            arguments = {
                "provider_code": "trafa",
                "dataset_code": "T10011|itrfslut|ar|drivm",
                "language": "sv",
                "selection": {"ar": ["2024"], "measure": ["itrfslut"]},
                "data_url": str(server.make_url("/api/data")),
                "metadata_url": None,
                "metadata": {},
            }
            first = await retrieval.retrieve(session, **arguments)
            with pytest.raises(LookupError):
                await retrieval.retrieve(
                    session, **{**arguments, "dataset_code": "T99999|x"}
                )
            second = await retrieval.retrieve(session, **arguments)
            return first, second, session.closed

    first, second, closed = asyncio.run(scenario())

    assert first == second
    assert closed is False

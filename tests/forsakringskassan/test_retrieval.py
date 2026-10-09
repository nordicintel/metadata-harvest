"""Retrieval against an imitation of the table endpoint built from recorded rows."""

import asyncio
from urllib.parse import unquote

import aiohttp
import pytest

from metadata_harvest.adapters.forsakringskassan import retrieval

AVSLUT = "ase-avslutsanledning/ASEAvslutsanledning"
LAN = "ase-beslut-lan/ASEBeslutLan"


def run(retrieve_from, fake, selection, **kwargs):
    return asyncio.run(retrieve_from(fake, selection, **kwargs))


def expected(fake, table, selection, measure_dimension="matt"):
    """Independently compute value/status maps from the recorded rows."""
    ids = list(selection)
    value, status = {}, {}
    for row in fake.tables[table]:
        dims = row["dimensions"]
        for measure, cell in row["observations"].items():
            coords = {**dims, measure_dimension: measure}
            if any(coords.get(name) not in selection[name] for name in ids):
                continue
            index = 0
            for name in ids:
                index = index * len(selection[name]) + selection[name].index(
                    coords[name]
                )
            if cell["value"] is not None:
                value[str(index)] = cell["value"]
            elif cell["rojd"]:
                value[str(index)], status[str(index)] = None, ".."
    return value, status


def as_map(value):
    return (
        {str(i): v for i, v in enumerate(value)} if isinstance(value, list) else value
    )


def test_values_and_suppressed_cells(fake, retrieve_from, check_fragment):
    selection = {
        "ar": ["2026"],
        "manad": ["09"],
        "beslut_kod": ["ALL", "A", "B"],
        "lan_kod": ["09", "23", "01"],
        "matt": ["antal"],
    }
    fragment = run(retrieve_from, fake, selection)

    check_fragment(fragment, selection)
    value, status = expected(fake, LAN, selection)
    assert as_map(fragment["value"]) == value
    assert fragment["status"] == status and set(status.values()) == {".."}
    assert all(as_map(fragment["value"])[k] is None for k in status)
    assert all(
        isinstance(v, int)
        for k, v in as_map(fragment["value"]).items()
        if k not in status
    )


def test_exact_request_shape(fake, retrieve_from):
    selection = {
        "ar": ["2026"],
        "manad": ["09"],
        "beslut_kod": ["ALL", "A"],
        "lan_kod": ["01", "03"],
        "matt": ["antal"],
    }
    run(retrieve_from, fake, selection)
    assert [unquote(r) for r in fake.requests[:1]] == [
        "/v1/ase-beslut-lan/ASEBeslutLan.json"
        "?ar=2026&manad=09&beslut_kod=ALL,A&lan_kod=01,03"
    ]
    assert len(fake.requests) == 2  # plus the session-reuse check


def test_not_reported_cells_are_left_out_and_dense_when_complete(
    fake, retrieve_from, check_fragment
):
    # Category 8 ("Övriga") has rojd: false with value null before 2018.
    selection = {
        "ar": ["2016", "2017"],
        "kon_kod": ["ALL"],
        "avslutsanledning_kod": ["7", "8"],
        "matt": ["antal"],
    }
    fragment = run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    check_fragment(fragment, selection)
    assert isinstance(fragment["value"], dict)
    assert set(fragment["value"]) == {"0", "2"}  # the "8" cells are left out
    assert "status" not in fragment

    selection["avslutsanledning_kod"] = ["5", "6", "7"]
    fragment = run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    check_fragment(fragment, selection)
    assert isinstance(fragment["value"], list) and len(fragment["value"]) == 6
    assert fragment["value"] == [
        as_map(expected(fake, AVSLUT, selection)[0])[str(i)] for i in range(6)
    ]


def test_selection_order_defines_fragment_order(fake, retrieve_from, check_fragment):
    selection = {
        "matt": ["antal"],
        "kon_kod": ["M", "K"],
        "avslutsanledning_kod": ["ALL"],
        "ar": ["2025", "2000"],
    }
    fragment = run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    check_fragment(fragment, selection)
    assert fragment["id"] == ["matt", "kon_kod", "avslutsanledning_kod", "ar"]
    assert as_map(fragment["value"]) == expected(fake, AVSLUT, selection)[0]
    row = next(
        r
        for r in fake.tables[AVSLUT]
        if r["dimensions"]["kon_kod"] == "M"
        and r["dimensions"]["ar"] == "2025"
        and r["dimensions"]["avslutsanledning_kod"] == "ALL"
    )
    assert as_map(fragment["value"])["0"] == row["observations"]["antal"]["value"]


def test_long_selections_are_split(fake, retrieve_from, check_fragment, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_QUERY_STRING", 60)
    years = [str(y) for y in range(1999, 2026)]
    selection = {
        "ar": years,
        "kon_kod": ["ALL", "K", "M"],
        "avslutsanledning_kod": ["ALL", "5"],
        "matt": ["antal"],
    }
    fragment = run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    check_fragment(fragment, selection)
    assert len(fake.requests) > 2
    assert as_map(fragment["value"]) == expected(fake, AVSLUT, selection)[0]


def test_unsplittable_selection_is_rejected(monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_QUERY_STRING", 5)
    with pytest.raises(ValueError, match="exceeds"):
        retrieval.split_selection({"ar": ["2025"], "kon_kod": ["ALL"]})


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ({"ar": ["2025"], "kon_kod": ["ALL"]}, "measures"),
        ({"matt": ["antal"]}, "at least one"),
    ],
)
def test_bad_selections_raise_value_error(fake, retrieve_from, selection, message):
    with pytest.raises(ValueError, match=message):
        run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    assert fake.requests == []


def test_missing_dimension_is_rejected(fake, retrieve_from):
    metadata = {
        "/id": ["ar", "kon_kod", "avslutsanledning_kod", "matt"],
        "/extension/forsakringskassan": {
            "report": "ase-avslutsanledning",
            "table": "ASEAvslutsanledning",
            "measure_dimension": "matt",
        },
    }
    with pytest.raises(ValueError, match=r"missing \['avslutsanledning_kod'\]"):
        run(
            retrieve_from,
            fake,
            {"ar": ["2025"], "kon_kod": ["ALL"], "matt": ["antal"]},
            dataset_code=AVSLUT,
            metadata=metadata,
        )


def test_no_rows_for_a_listed_table_is_empty(fake, retrieve_from, check_fragment):
    selection = {
        "ar": ["2026"],
        "kon_kod": ["ALL"],
        "avslutsanledning_kod": ["ALL"],
        "matt": ["antal"],
    }
    fragment = run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    check_fragment(fragment, selection)
    assert fragment["value"] == {} and "status" not in fragment


def test_gone_report_or_table_raises_lookup_error(fake, retrieve_from):
    selection = {"ar": ["2025"], "kon_kod": ["ALL"], "matt": ["antal"]}
    with pytest.raises(LookupError):  # unknown report: HTTP 404
        run(retrieve_from, fake, selection, dataset_code="gone-report/Gone")
    with pytest.raises(LookupError, match="no longer lists"):  # [] + meta check
        run(retrieve_from, fake, selection, dataset_code="ase-avslutsanledning/Gone")


def test_server_errors_propagate(fake, retrieve_from):
    fake.fail_status = 503
    selection = {"ar": ["2025"], "kon_kod": ["ALL"], "matt": ["antal"]}
    with pytest.raises(aiohttp.ClientResponseError):
        run(retrieve_from, fake, selection, dataset_code=AVSLUT)


def test_conflicting_or_foreign_rows_are_errors(fake, retrieve_from):
    selection = {
        "ar": ["2025"],
        "kon_kod": ["ALL"],
        "avslutsanledning_kod": ["ALL"],
        "matt": ["antal"],
    }
    rows = fake.tables[AVSLUT]
    row = next(r for r in rows if r["dimensions"]["ar"] == "2025")
    rows.append({**row, "observations": {"antal": {"rojd": False, "value": -1.0}}})
    with pytest.raises(RuntimeError, match="conflicting"):
        run(retrieve_from, fake, selection, dataset_code=AVSLUT)
    rows.pop()
    rows.append({**row, "dimensions": {**row["dimensions"], "kommun_kod": "0114"}})
    with pytest.raises(RuntimeError, match="differ"):
        run(retrieve_from, fake, selection, dataset_code=AVSLUT)


def test_module_imports_only_aiohttp_and_stdlib():
    import ast
    import sys
    from pathlib import Path

    tree = ast.parse(Path(retrieval.__file__).read_text(encoding="utf-8"))
    modules = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert modules - set(sys.stdlib_module_names) == {"aiohttp"}
    assert retrieval.REQUIRED_METADATA == ("/id", "/extension/forsakringskassan")

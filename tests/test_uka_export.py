"""Export parsing: layouts, hierarchy paths, category codes, values and rows."""

import gzip
from urllib.parse import urlencode

import pytest

from metadata_harvest.adapters.uka.export import (
    PATH_SEPARATOR,
    TOTAL,
    Dimension,
    category_code,
    category_path,
    parse_layout,
    parse_value,
    read_export,
)
from tests.uka_support import FIXTURES, RECORDED, UKA


def raw_export(indicator: int, period: str) -> str:
    """A recorded export decoded as plain UTF-8, so a BOM is kept."""
    query = urlencode(
        sorted({"export": "all", "export_type": "csv", "year": period}.items())
    )
    link = RECORDED["responses"][f"/api/totals/{indicator}?{query}"]["url"]
    entry = RECORDED["files"][link.removeprefix(UKA)]
    content = gzip.decompress((FIXTURES / "exports" / entry["file"]).read_bytes())
    return content.decode("utf-8")


# parse_layout


def test_layout_flat():
    layout = parse_layout(["Termin", "Lärosäte", "Program", "Kön", "Åldersgrupp", "X"])
    assert layout.period_column == "Termin"
    assert layout.measure_column == "X"
    assert layout.dimension_codes == ("Lärosäte", "Program", "Kön", "Åldersgrupp")
    assert layout.dimensions[0] == Dimension("Lärosäte", ("Lärosäte",))
    assert not any(dimension.hierarchical for dimension in layout.dimensions)


def test_layout_strips_header_cells():
    layout = parse_layout([" År ", " Kön", "Antal "])
    assert (layout.period_column, layout.measure_column) == ("År", "Antal")
    assert layout.dimension_codes == ("Kön",)


def test_layout_without_breakdowns():
    layout = parse_layout(["År", "Antal"])
    assert layout.dimensions == ()


def test_layout_hierarchy_of_three_levels():
    layout = parse_layout(["År", "Område[0]", "Ämne[1]", "Delämne[2]", "Kön", "N"])
    assert layout.dimensions == (
        Dimension("Område", ("Område[0]", "Ämne[1]", "Delämne[2]")),
        Dimension("Kön", ("Kön",)),
    )
    assert layout.dimensions[0].hierarchical
    assert not layout.dimensions[1].hierarchical


def test_layout_single_level_hierarchy():
    layout = parse_layout(["År", "Examen[0]", "Antal"])
    assert layout.dimensions == (Dimension("Examen", ("Examen[0]",)),)
    assert layout.dimensions[0].hierarchical


def test_layout_level_name_before_space():
    layout = parse_layout(["År", "Examen [0]", "Inriktning [1]", "Antal"])
    assert layout.dimension_codes == ("Examen",)
    assert layout.dimensions[0].columns == ("Examen [0]", "Inriktning [1]")


def test_layout_recorded_hierarchy():
    layout = parse_layout(
        "År;Lärosäte;Examen[0];Inriktning[1];Kön;Åldersgrupp;Antal".split(";")
    )
    assert layout.dimension_codes == ("Lärosäte", "Examen", "Kön", "Åldersgrupp")
    assert layout.dimensions[1].columns == ("Examen[0]", "Inriktning[1]")


def test_layout_adjacent_hierarchies_and_flat():
    header = ["År", "Land", "Grupp[0]", "Studieavgift[1]", "Nivå[0]", "Typ[1]", "N"]
    layout = parse_layout(header)
    assert layout.dimensions == (
        Dimension("Land", ("Land",)),
        Dimension("Grupp", ("Grupp[0]", "Studieavgift[1]")),
        Dimension("Nivå", ("Nivå[0]", "Typ[1]")),
    )


def test_layout_hierarchy_followed_by_flat():
    layout = parse_layout(["År", "Grupp[0]", "Studieavgift[1]", "Land", "Antal"])
    assert layout.dimensions == (
        Dimension("Grupp", ("Grupp[0]", "Studieavgift[1]")),
        Dimension("Land", ("Land",)),
    )


def test_layout_level_zero_starts_a_new_hierarchy():
    layout = parse_layout(["År", "Examen[0]", "Program[0]", "Inriktning[1]", "N"])
    assert layout.dimensions == (
        Dimension("Examen", ("Examen[0]",)),
        Dimension("Program", ("Program[0]", "Inriktning[1]")),
    )


@pytest.mark.parametrize(
    "header",
    [
        ["År", "Inriktning[1]", "Antal"],
        ["År", "Examen[0]", "Inriktning[2]", "Antal"],
        ["År", "Kön", "Inriktning[1]", "Antal"],
        ["År", "Examen[0]", "Inriktning[1]", "Kön", "Nivå[1]", "Antal"],
    ],
)
def test_layout_level_out_of_sequence(header):
    with pytest.raises(ValueError, match="out of sequence"):
        parse_layout(header)


@pytest.mark.parametrize(
    "header",
    [
        ["År", "Kön", "Kön", "Antal"],
        ["År", "Kön", " Kön ", "Antal"],
        ["År", "År", "Antal"],
        ["År", "Examen", "Examen[0]", "Inriktning[1]", "Antal"],
        ["År", "Examen[0]", "Kön", "Examen[0]", "Antal"],
    ],
)
def test_layout_duplicate_dimension_codes(header):
    with pytest.raises(ValueError, match="duplicate dimension codes"):
        parse_layout(header)


@pytest.mark.parametrize("header", [[], ["År"], ["År", "", "Antal"], ["År", " "]])
def test_layout_malformed_header(header):
    with pytest.raises(ValueError, match="unexpected UKÄ export header"):
        parse_layout(header)


# category_path and category_code


@pytest.mark.parametrize(
    ("cells", "path"),
    [
        ([""], ()),
        (["Total"], ()),
        (["Kvinnor"], ("Kvinnor",)),
        (["", ""], ()),
        (["Total", "Total"], ()),
        (["Total", ""], ()),
        (["Lärarexamina", ""], ("Lärarexamina",)),
        (["Lärarexamina", "Total"], ("Lärarexamina",)),
        (["A", "B"], ("A", "B")),
        (["A", "B", ""], ("A", "B")),
        (["A", "Total", "Total"], ("A",)),
        (["A", "B", "C"], ("A", "B", "C")),
        ([], ()),
    ],
)
def test_category_path(cells, path):
    assert category_path(cells) == path


@pytest.mark.parametrize(
    "cells", [["", "B"], ["Total", "B"], ["A", "", "C"], ["A", "Total", "C"]]
)
def test_category_path_skipping_a_level(cells):
    with pytest.raises(ValueError, match="skip a level"):
        category_path(cells)


def test_category_code():
    assert category_code(()) == TOTAL == "Total"
    assert category_code(("Kvinnor",)) == "Kvinnor"
    assert PATH_SEPARATOR == " > "
    assert (
        category_code(("Grundlärarexamina", "Grundlärarexamina, fritidshem"))
        == "Grundlärarexamina > Grundlärarexamina, fritidshem"
    )
    assert category_code(("A", "B", "C")) == "A > B > C"


# parse_value


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("96770031", (96770031, None)),
        ("-51", (-51, None)),
        ("30,5", (30.5, None)),
        ("-0,5", (-0.5, None)),
        ("0", (0, None)),
        ("0,0", (0.0, None)),
        ("..", (None, "..")),
        ("-", (None, "-")),
        ("1.5", (None, "1.5")),
        ("", None),
    ],
)
def test_parse_value(cell, expected):
    assert parse_value(cell) == expected


def test_parse_value_types():
    zero, _ = parse_value("0")
    assert type(zero) is int and zero == 0
    decimal_zero, _ = parse_value("0,0")
    assert type(decimal_zero) is float and decimal_zero == 0.0
    assert type(parse_value("30,5")[0]) is float
    assert type(parse_value("-51")[0]) is int


# read_export


def test_read_export_strips_bom():
    text = raw_export(67, "2025")
    assert text.startswith("﻿")
    layout, rows = read_export(text)
    assert layout.period_column == "År"
    assert layout.measure_column == "Andel"
    first = next(rows)
    assert first.period == "2025"
    assert first.categories == ("Total", "Kvinnor", "Total")
    assert first.value == 73


def test_read_export_quoted_cells_and_leading_space_period():
    layout, rows = read_export(raw_export(7, "2024/25"))
    assert layout.period_column == "Läsår"
    assert layout.measure_column == "AndelUbak"
    assert layout.dimension_codes == ("Lärosäte", "Kön", "Åldersgrupp")
    rows = list(rows)
    assert len(rows) == 137
    assert {row.period for row in rows} == {"2024/25"}
    assert rows[0].categories == ("Total", "Kvinnor", "Total")
    assert rows[0].paths == ((), ("Kvinnor",), ())
    assert (rows[0].value, rows[0].marker) == (30.5, None)
    assert rows[3].categories == (
        "Akademi för Ledarskap och Teologi",
        "Kvinnor",
        "Total",
    )
    assert (rows[3].value, rows[3].marker) == (None, "..")
    assert (rows[5].value, rows[5].marker) == (11.7, None)
    assert rows[-1].categories == ("Örebro universitet", "Total", "Total")
    assert rows[-1].value == 29.8


def test_read_export_unquoted_renamed_measure():
    layout, rows = read_export(raw_export(7, "2017/18"))
    assert layout.measure_column == "Andel"
    rows = list(rows)
    assert len(rows) == 141
    assert rows[0].period == "2017/18"
    assert rows[0].value == 25.3
    beckmans = [row for row in rows if row.categories[0] == "Beckmans Designhögskola"]
    assert {(row.value, row.marker) for row in beckmans} == {(None, "..")}


def test_read_export_two_flat_group_dimensions():
    layout, rows = read_export(raw_export(14, "2025"))
    assert layout.dimension_codes == (
        "Lärosäte",
        "Verksamhetsområde",
        "Finansiärsgrupp",
        "Kön",
        "Åldersgrupp",
    )
    assert not any(dimension.hierarchical for dimension in layout.dimensions)
    rows = {row.categories: row for row in rows}
    assert len(rows) == 1756
    assert rows[("Total",) * 5].value == 96770031
    assert rows[("Total", "Total", "Anslagsmedel", "Total", "Total")].value == 55809759
    gavle = rows[("Högskolan i Gävle", "Uppdragsforskning", "Övrigt", "Total", "Total")]
    assert (gavle.value, gavle.marker) == (-51, None)
    assert gavle.paths[1] == ("Uppdragsforskning",)


def test_read_export_hierarchy():
    layout, rows = read_export(raw_export(15, "2025"))
    assert layout.dimension_codes == ("Lärosäte", "Examen", "Kön", "Åldersgrupp")
    rows = {row.categories: row for row in rows}
    assert len(rows) == 2793
    assert rows[("Total", "Total", "Total", "Total")].value == 9626
    assert rows[("Total", "Lärarexamina", "Total", "Total")].value == 261
    code = "Grundlärarexamina > Grundlärarexamina, fritidshem"
    row = rows[("Total", code, "Total", "Total")]
    assert row.paths[1] == ("Grundlärarexamina", "Grundlärarexamina, fritidshem")
    assert row.value == 834
    assert rows[("Total", "Total", "Kvinnor", "-24")].value == 881


def test_read_export_absent_cells():
    _, rows = read_export(raw_export(97, "VT2026"))
    rows = {row.categories: row.value for row in rows}
    assert len(rows) == 2541
    nursing = ("Blekinge tekniska högskola", "Sjuksköterskeexamen")
    assert rows[(*nursing, "Män", "25-34")] == 6
    assert rows[(*nursing, "Män", "Total")] == 17
    assert (*nursing, "Män", "35-") not in rows
    assert rows[(*nursing, "Total", "35-")] == 22


def test_read_export_skips_blank_measure_and_empty_lines():
    text = raw_export(7, "2024/25").rstrip("\r\n")
    text += '\r\n" 2024/25";"Nowhere";"Kvinnor";"Total";""\r\n\r\n'
    text += '" 2024/25";"Nowhere";"Män";"Total";"   "\r\n'
    _, rows = read_export(text)
    rows = list(rows)
    assert len(rows) == 137
    assert all(row.categories[0] != "Nowhere" for row in rows)


def test_read_export_wrong_cell_count_raises_while_iterating():
    text = raw_export(67, "2025").rstrip("\r\n") + "\r\n2025;Extra;Kvinnor;42\r\n"
    layout, rows = read_export(text)
    assert layout.measure_column == "Andel"
    with pytest.raises(ValueError, match=r"export line 140 has 4 cells, not 5"):
        list(rows)


def test_read_export_skip_level_raises_while_iterating():
    text = "År;Examen[0];Inriktning[1];Antal\n2025;;Förskollärare;3\n"
    _, rows = read_export(text)
    with pytest.raises(ValueError, match="skip a level"):
        list(rows)


@pytest.mark.parametrize("text", ["", "﻿"])
def test_read_export_empty(text):
    with pytest.raises(ValueError, match="empty UKÄ export"):
        read_export(text)


def test_read_export_malformed_header():
    with pytest.raises(ValueError, match="out of sequence"):
        read_export("År;Inriktning[1];Antal\n2025;X;1\n")

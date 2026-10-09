"""Catalogue and ``/api/totals/{id}`` parsing against recorded UKÄ responses."""

import copy

import pytest

from metadata_harvest.adapters.uka.source import (
    Indicator,
    documentation_url,
    is_discontinued,
    parse_catalogue,
    parse_totals,
    plain_text,
    totals_url,
)
from tests.uka_support import RECORDED

RESPONSES = RECORDED["responses"]
CATALOGUE = RESPONSES["/api/indicators"]


def _indicator(description=None, measurement=None) -> Indicator:
    return Indicator(1, "Namn", description, measurement, None, ())


def test_catalogue_lists_every_indicator_once_in_its_group():
    entries = parse_catalogue(CATALOGUE)

    assert len(entries) == 113
    assert len({entry.id for entry in entries}) == 113
    assert list(dict.fromkeys(entry.group for entry in entries)) == [
        "Utbildning på grundnivå och avancerad nivå",
        "Nyckeltal",
        "Personal",
        "Ekonomi",
        "Utbildning på forskarnivå",
        "Forskning vid universitet och högskolor",
        "Etablering på arbetsmarknaden",
    ]
    first = entries[0]
    assert (first.id, first.name, first.group, first.most_recent_url) == (
        1,
        "Registrerade studenter per ämnesgrupp (termin)",
        "Utbildning på grundnivå och avancerad nivå",
        "https://statistik-api.uka.se/api/totals/1/?year=HT2025",
    )
    by_id = {entry.id: entry for entry in entries}
    assert by_id[43].group == "Utbildning på forskarnivå"
    assert by_id[7].group == "Nyckeltal"


def test_catalogue_group_falls_back_to_the_key():
    body = {" Grupp ": [{"id": "5", "name": " Namn "}]}

    [entry] = parse_catalogue(body)

    assert (entry.id, entry.name, entry.group, entry.most_recent_url) == (
        5,
        "Namn",
        "Grupp",
        None,
    )


def test_catalogue_rejects_duplicate_ids():
    body = copy.deepcopy(CATALOGUE)
    groups = list(body)
    body[groups[1]].append(body[groups[0]][0])

    with pytest.raises(ValueError, match="more than once"):
        parse_catalogue(body)


@pytest.mark.parametrize("body", [[], "error", None, {"Grupp": {"id": 1}}])
def test_catalogue_rejects_unexpected_bodies(body):
    with pytest.raises(ValueError):
        parse_catalogue(body)


def test_totals_of_a_yearly_indicator():
    indicator = parse_totals(RESPONSES["/api/totals/67"], 67)

    assert indicator.id == 67
    assert indicator.name == (
        "Andel doktorsexaminerad forskande och undervisande personal"
    )
    assert indicator.measurement == (
        "Doktorsexaminerad (%) forskande och undervisande personal"
    )
    assert indicator.current_year == "2025"
    assert indicator.periods == tuple(str(year) for year in range(2012, 2026))


def test_totals_of_an_academic_year_indicator():
    indicator = parse_totals(RESPONSES["/api/totals/72"], 72)

    assert indicator.name == "Andel examinerade med utlandsstudier"
    assert indicator.current_year == "2024/25"
    assert len(indicator.periods) == 23
    assert indicator.periods[:2] == ("2002/03", "2003/04")
    assert indicator.periods[-1] == "2024/25"
    assert list(indicator.periods) == sorted(indicator.periods)


def test_totals_strips_periods_and_texts():
    body = copy.deepcopy(RESPONSES["/api/totals/72"])
    body["yearly_totals"]["totals"][-1]["year"] = " 2024/25 "
    body["indicator"]["current_year"] = "  "
    body["indicator"]["measurement"] = None

    indicator = parse_totals(body, 72)

    assert indicator.periods[-1] == "2024/25"
    assert indicator.current_year is None
    assert indicator.measurement is None


def test_totals_error_body_raises_lookup_error():
    with pytest.raises(LookupError, match="Indicator has no datasets."):
        parse_totals({"error": "Indicator has no datasets."}, 3)


def test_totals_rejects_malformed_bodies():
    body = copy.deepcopy(RESPONSES["/api/totals/67"])
    del body["yearly_totals"]
    duplicated = copy.deepcopy(RESPONSES["/api/totals/67"])
    totals = duplicated["yearly_totals"]["totals"]
    totals.append(totals[0])

    for bad in ([], body, duplicated):
        with pytest.raises(ValueError):
            parse_totals(bad, 67)


def test_totals_url():
    assert totals_url(97) == "https://statistik-api.uka.se/api/totals/97"


def test_plain_text_strips_markup():
    description = RESPONSES["/api/totals/97"]["indicator"]["description"]

    assert plain_text(description) == (
        "Antal antagna till yrkesexamensprogram. Beskrivning av statistiken"
    )
    assert plain_text("<p>Kvinnor &amp;\n  m&auml;n</p>") == "Kvinnor & män"
    assert plain_text("<br/> ") is None
    assert plain_text(None) is None


def test_documentation_url_is_the_first_absolute_link():
    description = RESPONSES["/api/totals/67"]["indicator"]["description"]

    assert documentation_url(description) == (
        "https://www.uka.se/statistik-och-analys/hogskolan-i-siffror/"
        "beskrivningar-av-statistiken/personal"
        "?plusbox=12_4e1e1cc819b2c0041ba149_button"
    )
    assert (
        documentation_url(
            "<a href=\"/relativ\">x</a> <A HREF='https://a.se/?b=1&amp;c=2'>y</A>"
            ' <a href="https://b.se">z</a>'
        )
        == "https://a.se/?b=1&c=2"
    )
    assert documentation_url("Ingen länk") is None
    assert documentation_url(None) is None


def test_is_discontinued():
    assert is_discontinued(
        _indicator(description="Text. OBS! Statistiken uppdateras inte längre.")
    )
    assert is_discontinued(
        _indicator(measurement="Doktorandnybörjare (UPPDATERAS INTE LÄNGRE)")
    )
    assert not is_discontinued(_indicator())
    assert not is_discontinued(parse_totals(RESPONSES["/api/totals/97"], 97))

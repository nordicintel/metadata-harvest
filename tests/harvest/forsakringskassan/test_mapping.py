"""Catalogue, meta.json and texts parsing, and the Dataset mapping."""

import copy

import pytest

from apps.harvest.metadata_adapters.forsakringskassan.api import (
    REPORTS_URL,
    TEXTS_URL,
    meta_url,
)
from apps.harvest.metadata_adapters.forsakringskassan.mapping import (
    STATUS_NOTE,
    build_dataset,
)
from apps.harvest.metadata_adapters.forsakringskassan.source import (
    Listed,
    catalogue,
    fingerprint,
    parse_meta,
    plain,
    probe_filters,
    stated_unit,
    text_entries,
    undeclared_dimensions,
)
from apps.harvest.validation import validate_metadata
from tests.support.forsakringskassan import RecordedClient


@pytest.fixture
def recorded():
    return RecordedClient()


def documents(recorded, name, group="ase"):
    meta = recorded.body(meta_url(name))
    texts = recorded.body(TEXTS_URL)
    report = parse_meta(meta, name, "fn")
    built = {}
    for table in report.tables:
        document = build_dataset(
            report,
            table,
            Listed(name, "fn", group),
            texts,
            meta_sha256=fingerprint(meta),
            texts_sha256=fingerprint(text_entries(texts, report)),
        )
        validate_metadata(document, adapter="forsakringskassan")
        built[table.name] = document
    return built


def test_single_table_report(recorded):
    (document,) = documents(recorded, "ase-avslutsanledning").values()
    shared = document["extension"]["nordicintel"]
    native = document["extension"]["forsakringskassan"]

    assert document["label"] == "Antal avslutade assistansersättningar"
    assert document["source"] == "Försäkringskassan"
    assert "updated" not in document
    assert document["id"] == ["ar", "kon_kod", "avslutsanledning_kod", "matt"]
    assert document["size"] == [27, 3, 5, 1]
    assert document["role"] == {"time": ["ar"], "metric": ["matt"]}
    assert document["value"] == [] and "status" not in document
    assert document["note"][0].startswith("Årsstatistik från och med 1999")
    assert document["note"][-1] == STATUS_NOTE
    kon = document["dimension"]["kon_kod"]
    assert kon["category"]["label"]["ALL"] == "Kvinnor och män"
    assert kon["extension"]["nordicintel"] == {
        "elimination": True,
        "elimination_value": "ALL",
    }
    assert kon["extension"]["forsakringskassan"] == {"concept": "sex"}
    assert document["dimension"]["ar"]["extension"]["nordicintel"] == {
        "elimination": False
    }
    matt = document["dimension"]["matt"]
    assert matt["category"]["label"] == {"antal": "Antal avslutade ersättningar"}
    assert "unit" not in matt["category"]  # no unit stated for "antal"
    assert matt["category"]["note"]["antal"][0].startswith("Antal avslutade ersättn")
    assert shared == {
        **shared,
        "provider_code": "forsakringskassan",
        "dataset_code": "ase-avslutsanledning/ASEAvslutsanledning",
        "language": "sv",
        "official_statistics": True,
        "time_unit": "annual",
        "first_period": "1999",
        "last_period": "2025",
        "subject": {"code": "ase", "label": "Assistansersättning"},
        "paths": [
            [
                {"id": "fn", "label": "Statistik inom området funktionsnedsättning"},
                {"id": "ase", "label": "Assistansersättning"},
            ]
        ],
        "source_url": "https://www.forsakringskassan.se/statistik-och-analys/"
        "statistikdatabas#!/fn/ase-avslutsanledning",
        "metadata_url": "https://www.forsakringskassan.se/api/sprstatistikrapportera/"
        "public/v1/ase-avslutsanledning/meta.json",
        "data_url": "https://www.forsakringskassan.se/api/sprstatistikrapportera/"
        "public/v1/ase-avslutsanledning/ASEAvslutsanledning.json",
        "contacts": [
            {
                "organization": "Försäkringskassan",
                "email": "statistikenheten@forsakringskassan.se",
            }
        ],
    }
    assert "<" not in shared["description"] and "&lt;" not in shared["description"]
    assert "discontinued" not in shared
    assert native["report"] == "ase-avslutsanledning"
    assert native["table"] == "ASEAvslutsanledning"
    assert native["measure_dimension"] == "matt"
    record = native["source_record"]
    assert record["catalogue"] == {
        "report": "ase-avslutsanledning",
        "area": "fn",
        "group": "ase",
    }
    assert record["meta"]["revision"] == 0 and len(record["meta_sha256"]) == 64


def test_two_table_report_splits_grouped_dimensions(recorded):
    built = documents(recorded, "ase-beslut-lan")
    kon, lan = built["ASEBeslutKon"], built["ASEBeslutLan"]

    assert kon["label"] == "Antal beviljanden och avslag vid förstagångsbeslut: Kön"
    assert lan["label"].endswith(": Län")
    assert kon["id"] == ["ar", "manad", "beslut_kod", "kon_kod", "matt"]
    assert lan["id"] == ["ar", "manad", "beslut_kod", "lan_kod", "matt"]
    assert lan["role"] == {
        "time": ["ar", "manad"],
        "geo": ["lan_kod"],
        "metric": ["matt"],
    }
    assert lan["extension"]["nordicintel"]["time_unit"] == "monthly"
    assert "first_period" not in lan["extension"]["nordicintel"]
    assert lan["dimension"]["lan_kod"]["extension"]["forsakringskassan"] == {
        "group": "uppdelning_lan",
        "concept": "county",
    }
    assert lan["extension"]["forsakringskassan"]["computed"] == [
        {"data": "${ar} ${manad}", "key": "tidsperiod", "label": "Tidsperiod"}
    ]
    links = lan["link"]["describedby"]
    assert all(
        link["href"].startswith("https://www.forsakringskassan.se/download/")
        for link in links
    )
    assert lan["extension"]["nordicintel"]["doc_url"] == next(
        link["href"] for link in links if "kvalitetsdeklaration" in link["href"]
    )


def test_key_alias_variants_keep_ids_and_share_roles(recorded):
    (document,) = documents(recorded, "syn-alias", "syn").values()

    assert document["id"] == ["ar", "manad", "beslut_kod", "kon", "lan", "matt"]
    # "ar" labelled "Barnets födelseår" is a birth year, not the time role.
    assert document["role"] == {"time": ["manad"], "geo": ["lan"], "metric": ["matt"]}
    assert document["dimension"]["lan"]["extension"]["forsakringskassan"] == {
        "group": "uppdelning_lan",
        "concept": "county",
    }
    assert document["dimension"]["kon"]["extension"]["forsakringskassan"] == {
        "concept": "sex"
    }
    assert document["dimension"]["matt"]["category"]["unit"] == {
        "belopp": {"label": "1000-tal kr"}
    }
    assert document["extension"]["nordicintel"]["official_statistics"] is False
    assert document["extension"]["nordicintel"]["subject"] == {
        "code": "ase",
        "label": "Assistansersättning",
    }


def test_missing_report_text_is_a_metadata_failure(recorded):
    texts = recorded.body(TEXTS_URL)
    del texts["MEKA.report.ase-avslutsanledning"]
    meta = recorded.body(meta_url("ase-avslutsanledning"))
    report = parse_meta(meta, "ase-avslutsanledning", "fn")
    with pytest.raises(ValueError, match="no name"):
        build_dataset(
            report,
            report.tables[0],
            Listed("ase-avslutsanledning", "fn", "ase"),
            texts,
            meta_sha256="x",
            texts_sha256="y",
        )


def test_discontinued_report_names(recorded):
    texts = recorded.body(TEXTS_URL)
    texts["MEKA.report.ase-avslutsanledning"]["name"] += " (uppdateras ej)"
    meta = recorded.body(meta_url("ase-avslutsanledning"))
    report = parse_meta(meta, "ase-avslutsanledning", "fn")
    document = build_dataset(
        report,
        report.tables[0],
        Listed("ase-avslutsanledning", "fn", "ase"),
        texts,
        meta_sha256="x",
        texts_sha256="y",
    )
    assert document["extension"]["nordicintel"]["discontinued"] is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m.update(key="other"), "describes"),
        (
            lambda m: m["filter"]["dimension"].append(m["filter"]["dimension"][0]),
            "duplicate",
        ),
        (lambda m: m["filter"]["dimension"][0].update(key="matt"), "reserved"),
        (lambda m: m["filter"].update(matt=[]), "no measures"),
        (lambda m: m["filter"].update(uppdelning=[]), "no tables"),
        (lambda m: m.pop("filter"), "no filter"),
    ],
)
def test_malformed_meta_is_rejected(recorded, change, message):
    meta = copy.deepcopy(recorded.body(meta_url("ase-avslutsanledning")))
    change(meta)
    with pytest.raises(ValueError, match=message):
        parse_meta(meta, "ase-avslutsanledning", "fn")


def test_duplicate_tables_are_set_aside(recorded):
    meta = copy.deepcopy(recorded.body(meta_url("ase-beslut-lan")))
    meta["filter"]["uppdelning"].append(dict(meta["filter"]["uppdelning"][1]))
    report = parse_meta(meta, "ase-beslut-lan", "fn")
    assert report.duplicate_tables == ("ASEBeslutLan",)
    assert [t.name for t in report.tables] == ["ASEBeslutKon"]


def test_undeclared_columns_and_probe(recorded):
    meta = recorded.body(meta_url("syn-kommun"))
    report = parse_meta(meta, "syn-kommun", "fn")
    assert report.undeclared == ("kommun_kod",)  # tidsperiod is computed
    lan = report.tables[1]
    assert probe_filters(report, lan) == {
        "ar": ["2026"],
        "manad": ["01"],
        "beslut_kod": ["ALL"],
        "lan_kod": ["ALL"],
    }
    row = {"dimensions": {**{k: "x" for k in lan.dimensions}, "kommun_kod": "ALL_01"}}
    assert undeclared_dimensions([row], lan) == ["kommun_kod"]
    with pytest.raises(ValueError, match="no rows"):
        undeclared_dimensions([], lan)
    with pytest.raises(ValueError, match="lack"):
        undeclared_dimensions([{"dimensions": {"ar": "2026"}}], lan)
    assert (
        parse_meta(
            recorded.body(meta_url("ase-beslut-lan")), "ase-beslut-lan", "fn"
        ).undeclared
        == ()
    )


def test_catalogue_isolates_malformed_and_duplicate_rows(recorded):
    body = recorded.body(REPORTS_URL)
    body.insert(0, {"area": "fn", "key": "broken"})  # synthetic: no reports list
    body.append({"area": "bf", "key": "dup", "reports": ["ase-beslut-lan", 7]})
    problems = []
    listed, complete = catalogue(body, problems)
    assert not complete
    assert [item.report for item in listed] == [
        "ase-avslutsanledning",
        "syn-alias",
        "syn-kommun",
    ]
    assert [resource for resource, _ in problems] == ["#0", "dup#3", "ase-beslut-lan"]
    with pytest.raises(ValueError):
        catalogue({"not": "a list"}, [])


def test_texts_helpers():
    assert plain(
        "&lt;p&gt;Belopp &amp;auml;r&lt;/p&gt;&lt;p&gt;utbetalt&lt;/p&gt;"
    ) == ("Belopp är utbetalt")
    assert stated_unit("Belopp i 1000-tal kr") == "1000-tal kr"
    assert stated_unit("Belopp i 1 000-tal kronor") == "1 000-tal kronor"
    assert stated_unit("Slutligt avstämda bidragsbelopp, tusentals kronor") == (
        "tusentals kronor"
    )
    assert stated_unit("Genomsnittligt bidragsbelopp i kr") == "kr"
    assert stated_unit("Andel mottagare per kön (%)") == "%"
    assert stated_unit("Antal mottagare") is None
    assert stated_unit("Belopp") is None
    assert fingerprint({"b": 1, "a": [1, 2]}) == fingerprint({"a": [1, 2], "b": 1})

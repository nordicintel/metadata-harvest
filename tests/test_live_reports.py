import runpy


def test_matrix_summary_requires_every_combination_once():
    summarize = runpy.run_path("scripts/summarize_pxweb_live.py")["summarize"]
    providers = [{"code": "p", "languages": ["sv"], "database_ids": ["db"]}]
    item = {
        "provider": "p",
        "language": "sv",
        "database_id": "db",
        "equivalent": True,
        "web": {"status": "success", "count": 3},
        "api": {"status": "success", "count": 3},
    }
    report = {"accepted": True, "combinations": [item]}
    assert summarize(providers, [report])["accepted"]
    assert not summarize(providers, [])["accepted"]
    assert not summarize(providers, [report, report])["accepted"]
    assert not summarize(providers, [{**report, "accepted": False}])["accepted"]


def test_hidden_discovery_database_does_not_hide_record_mismatches():
    accepted = runpy.run_path("scripts/check_pxweb_live.py")["accepted"]
    report = {
        "expected_combinations": 1,
        "combinations": [
            {
                "equivalent": True,
                "web": {"status": "success"},
                "api": {"status": "success"},
            }
        ],
        "discovery": [
            {
                "status": "success",
                "missing": ["Hidden"],
                "configured_records_equal": False,
                "visible_records_equal": True,
            }
        ],
    }
    assert accepted(report)
    report["discovery"][0]["visible_records_equal"] = False
    assert not accepted(report)
    assert not accepted({"combinations": [], "discovery": []})

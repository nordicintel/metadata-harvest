"""Jobs fail only on whole-scope failures; individual errors stay green."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from metadata_harvest.outcome import REPORTS, scope_failures

ROOT = Path(__file__).resolve().parents[1]


def test_individual_errors_do_not_fail_any_adapter():
    individual = {
        "pxweb_v1": {
            "scopes": [{"provider_code": "scb", "language": "sv", "complete": True}],
            "errors": [{"x": 1}],
        },
        "pxweb_v2": {
            "scopes": [{"language": "sv", "complete": True}],
            "errors": [{"stage": "codelist"}] * 55,
        },
        "kolada": {"errors": [{"stage": "metadata", "resource": "ou/N1"}]},
        "uka": {
            "errors": [
                {"stage": "endpoint", "resource": "97"},
                {"stage": "discovery", "resource": "999"},
            ]
        },
        "sdb": {"errors": [{"stage": "metadata", "resource": "sv/amning"}]},
        "trafa": {"errors": [{"stage": "endpoint", "resource": "sv/T10011|x"}]},
        "entrysoft": {
            "errors": [{"stage": "metadata", "resource": "a/1"}],
            "interrupted": "x: request budget of 40 used up",
        },
        "smhi_emissions": {
            "errors": [
                {"stage": "endpoint", "resource": "sv/emissionsdatabasen_NOx"},
                {"stage": "metadata", "resource": "sv/emissionsdatabasen_CO2"},
                {"stage": "discovery", "resource": "filteroptions/4.1/3"},
                {"stage": "discovery", "resource": "*/XYZ"},
            ]
        },
        "catalog": {
            "errors": [],
            "upstream": {
                "pxweb_v2": {"harvest.json": {"status": "completed_with_errors"}}
            },
        },
    }
    for adapter, report in individual.items():
        assert scope_failures(adapter, report) == [], adapter


def test_whole_scope_failures_fail():
    failing = {
        "pxweb_v1": {
            "scopes": [{"provider_code": "scb", "language": "en", "complete": False}]
        },
        "pxweb_v2": {"scopes": [{"language": "en", "complete": False}]},
        "kolada": {"errors": [{"stage": "discovery", "resource": "kpi"}]},
        "sdb": {"errors": [{"stage": "discovery", "resource": "en"}]},
        "trafa": {"errors": [{"stage": "endpoint", "resource": "*/T10011"}]},
        "entrysoft": {
            "errors": [{"stage": "discovery", "resource": "catalogalese"}],
            "interrupted": None,
        },
        "smhi_emissions": {
            "errors": [{"stage": "discovery", "resource": "filteroptions"}]
        },
        "catalog": {
            "errors": [
                {
                    "collection": "kolada",
                    "error": "Missing accepted metadata collections",
                }
            ]
        },
    }
    for adapter, report in failing.items():
        assert scope_failures(adapter, report), adapter
    assert scope_failures(
        "entrysoft", {"errors": [], "interrupted": "host: HTTP 429 received 2 times"}
    )


def test_command_reads_the_published_report(tmp_path):
    (tmp_path / "sdb/reports").mkdir(parents=True)
    (tmp_path / "sdb/reports/latest.json").write_text(
        json.dumps(
            {
                "status": "completed_with_errors",
                "counts": {},
                "errors": [{"stage": "metadata", "resource": "sv/x"}],
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "metadata_harvest.outcome",
            "--adapter",
            "sdb",
            "--data",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and '"scope_failures": []' in result.stdout


@pytest.mark.parametrize("adapter", sorted(REPORTS))
def test_every_workflow_uses_the_outcome_command(adapter):
    name = (
        "catalog.yml"
        if adapter == "catalog"
        else {"pxweb_v1": "harvest.yml"}.get(
            adapter, f"harvest-{adapter.replace('_', '-')}.yml"
        )
    )
    text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    assert f"metadata_harvest.outcome --adapter {adapter}" in text
    assert "completed_with_errors" not in text

"""Jobs fail only when a whole adapter failed; other errors stay green."""

import json
import os
import subprocess
import sys

import pytest

from apps.operations.outcome import REPORTS, adapter_failed, scope_failures
from apps.paths import ROOT


@pytest.mark.parametrize(
    "adapter,whole_scope,individual",
    [
        ("forsakringskassan", ["reports", "texts"], "reports/#0"),
        ("dataanalys", ["charts", "dashboards"], "charts/2541"),
        ("riksbank", ["*/swea", "*/mpd", "*/swestr"], "swea/series.0"),
        ("tillvaxtverket", [], "Nope"),
    ],
)
def test_new_adapter_scope_failures(adapter, whole_scope, individual):
    assert (
        scope_failures(
            adapter, {"errors": [{"stage": "discovery", "resource": individual}]}
        )
        == []
    )
    for resource in whole_scope:
        assert scope_failures(
            adapter, {"errors": [{"stage": "discovery", "resource": resource}]}
        ) == [resource]
    assert (
        scope_failures(
            adapter, {"errors": [{"stage": "endpoint", "resource": "sv/table"}]}
        )
        == []
    )
    if adapter in {"forsakringskassan", "dataanalys"}:
        assert scope_failures(
            adapter, {"errors": [{"stage": "endpoint", "resource": "*/area"}]}
        ) == ["*/area"]


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
                {"stage": "endpoint", "resource": "sv/97"},
                {
                    "stage": "discovery",
                    "resource": "*/999",
                    "error": "LookupError: Unknown UKÄ indicator",
                },
            ]
        },
        "sdb": {"errors": [{"stage": "metadata", "resource": "sv/amning"}]},
        "trafa": {"errors": [{"stage": "endpoint", "resource": "sv/T10011|x"}]},
        "entryscape": {
            "errors": [{"stage": "metadata", "resource": "a/1"}],
            "interrupted": "x: request budget of 40 used up",
        },
        "smhi_emissions": {
            "errors": [
                {"stage": "endpoint", "resource": "sv/emissionsdatabasen_NOx"},
                {"stage": "metadata", "resource": "sv/emissionsdatabasen_CO2"},
                {"stage": "discovery", "resource": "filteroptions/4.1/3"},
                {
                    "stage": "discovery",
                    "resource": "*/XYZ",
                    "error": "LookupError: Unknown SMHI substance",
                },
            ]
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
        "sdb": {"errors": [{"stage": "endpoint", "resource": "*/en"}]},
        "trafa": {"errors": [{"stage": "endpoint", "resource": "*/T10011"}]},
        "entryscape": {
            "errors": [{"stage": "discovery", "resource": "catalogalese"}],
            "interrupted": None,
        },
        "smhi_emissions": {
            "errors": [{"stage": "endpoint", "resource": "*/filteroptions"}]
        },
    }
    for adapter, report in failing.items():
        assert scope_failures(adapter, report), adapter
    assert scope_failures(
        "entryscape", {"errors": [], "interrupted": "host: HTTP 429 received 2 times"}
    )


def test_only_a_whole_adapter_failure_fails():
    partial = {
        "pxweb_v2": {
            "scopes": [
                {"language": "sv", "complete": True},
                {"language": "en", "complete": False},
            ]
        },
        "entryscape": {
            "counts": {"unchanged": 528, "harvested": 29},
            "errors": [{"stage": "discovery", "resource": "falunentryscapenet"}],
        },
        "riksbank": {
            "counts": {"unchanged": 34},
            "errors": [{"stage": "endpoint", "resource": "*/swea"}],
        },
    }
    for adapter, report in partial.items():
        failures = scope_failures(adapter, report)
        assert failures and not adapter_failed(adapter, report, failures), adapter
    whole = {
        "pxweb_v2": {"scopes": [{"language": "en", "complete": False}]},
        "kolada": {
            "counts": {"reused": 9, "checked": 0},
            "errors": [{"stage": "discovery", "resource": "kpi"}],
        },
        "riksbank": {
            "counts": {"unchanged": 0, "withheld": 42},
            "errors": [
                {"stage": "endpoint", "resource": f"*/{api}"}
                for api in ("swea", "mpd", "swestr")
            ],
        },
    }
    for adapter, report in whole.items():
        failures = scope_failures(adapter, report)
        assert adapter_failed(adapter, report, failures), adapter
    assert not adapter_failed("sdb", {"counts": {}}, [])


def test_command_warns_but_succeeds_on_a_partial_failure(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    (tmp_path / "entryscape/reports").mkdir(parents=True)
    (tmp_path / "entryscape/reports/latest.json").write_text(
        json.dumps(
            {
                "status": "completed_with_errors",
                "counts": {"unchanged": 528},
                "errors": [{"stage": "discovery", "resource": "falunentryscapenet"}],
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.operations.outcome",
            "--adapter",
            "entryscape",
            "--data",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
    )
    assert result.returncode == 0
    assert "::warning" in result.stdout and "falunentryscapenet" in result.stdout
    assert "falunentryscapenet" in summary.read_text(encoding="utf-8")


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
            "apps.operations.outcome",
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
    name = {"pxweb_v1": "harvest.yml"}.get(
        adapter, f"harvest-{adapter.replace('_', '-')}.yml"
    )
    text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    assert f"apps.operations.outcome --adapter {adapter}" in text
    assert "completed_with_errors" not in text

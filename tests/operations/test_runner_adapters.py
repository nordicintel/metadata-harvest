"""New adapters are reachable through the same CLI used by hosted jobs."""

import importlib
import sys
from pathlib import Path

import pytest

import apps.operations.harvest as runner


@pytest.mark.parametrize(
    "adapter,flags,expected",
    [
        (
            "smhi_emissions",
            ["--subject", "CO2"],
            {"subjects": ["CO2"], "languages": None},
        ),
        (
            "riksbank",
            ["--subject", "swestr"],
            {"subjects": ["swestr"], "languages": None},
        ),
        (
            "dataanalys",
            ["--subject", "GONV", "--indicator", "G082"],
            {"areas": ["GONV"], "indicators": ["G082"], "languages": None},
        ),
        (
            "tillvaxtverket",
            ["--product", "GuestNights_Country_Year"],
            {"products": ["GuestNights_Country_Year"], "languages": None},
        ),
        (
            "forsakringskassan",
            ["--subject", "ase-beslut-lan", "--full"],
            {"subjects": ["ase-beslut-lan"], "full": True, "languages": None},
        ),
    ],
)
def test_run_and_collect_use_adapter_configuration(
    monkeypatch, adapter, flags, expected
):
    workflow = importlib.import_module(
        f"apps.harvest.metadata_adapters.{adapter}.workflow"
    )
    calls = []
    result = {
        "run_id": "test-run",
        "status": "success",
        "continue": False,
        "counts": {},
    }

    async def run(provider, data, output, **kwargs):
        calls.append((provider, data, output, kwargs))
        return result

    def collect(data, output, **kwargs):
        calls.append((data, output, kwargs))
        return result

    monkeypatch.setattr(workflow, "run", run)
    monkeypatch.setattr(workflow, "collect", collect)
    monkeypatch.setattr(sys, "argv", ["runner", "run", "--adapter", adapter, *flags])
    runner.main()
    provider, data, output, options = calls.pop()
    assert data == Path("data") / adapter
    assert output == Path("output") / adapter
    assert options == expected
    assert provider.options["languages"]

    monkeypatch.setattr(sys, "argv", ["runner", "collect", "--adapter", adapter])
    runner.main()
    assert calls.pop() == (data, output, {"languages": provider.options["languages"]})


@pytest.mark.parametrize(
    "adapter", ["smhi_emissions", "riksbank", "dataanalys", "tillvaxtverket"]
)
def test_unsupported_full_flag_is_rejected_before_source_requests(monkeypatch, adapter):
    def unexpected_run(args):
        pytest.fail("Unsupported full mode reached the worker")

    monkeypatch.setattr(runner, "run_simple", unexpected_run)
    monkeypatch.setattr(sys, "argv", ["runner", "run", "--adapter", adapter, "--full"])
    with pytest.raises(SystemExit, match="2"):
        runner.main()

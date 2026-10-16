"""Decide whether a finished job should fail: only when a whole adapter failed.

Individual table, codelist, indicator or dataset errors are reported in the
published reports and stay visible there; they never fail a job. A failed scope
(a provider, language or base API that could not be read) is reported as a
warning while the rest of the adapter still produced results. A job fails only
when the adapter as a whole produced nothing: every scope failed, or a scope
failed and no item was read from the source in the run. The catalog fails only
when no adapter's collections could be taken at all.
"""

import argparse
import json
import os
from pathlib import Path

from apps.harvest.metadata_adapters import load
from apps.harvest.storage import read

REPORTS = {
    "pxweb_v1": "reports/refresh.json",
    "pxweb_v2": "reports/harvest.json",
    "kolada": "reports/latest.json",
    "uka": "reports/latest.json",
    "sdb": "reports/latest.json",
    "trafa": "reports/latest.json",
    "entryscape": "reports/latest.json",
    "smhi_emissions": "reports/latest.json",
    "forsakringskassan": "reports/latest.json",
    "riksbank": "reports/latest.json",
    "dataanalys": "reports/latest.json",
    "tillvaxtverket": "reports/latest.json",
}


def _incomplete_scopes(report: dict) -> list[str]:
    return [
        "/".join(
            str(scope.get(k)) for k in ("provider_code", "language") if scope.get(k)
        )
        for scope in report.get("scopes") or []
        if not scope.get("complete")
    ]


def _discovery(report: dict, *, resource=None) -> list[str]:
    return [
        e["resource"]
        for e in report.get("errors") or []
        if e.get("stage") == "discovery"
        and "Unknown" not in str(e.get("error", ""))  # a misspelled manual selection
        and (resource is None or resource(e["resource"]))
    ]


def _scoped(report: dict, *, discovery_resources=(), endpoint_prefix=None) -> list[str]:
    """Named discovery resources, or endpoint errors of a whole scope, that failed."""
    return [
        e["resource"]
        for e in report.get("errors") or []
        if (e.get("stage") == "discovery" and e["resource"] in discovery_resources)
        or (
            endpoint_prefix is not None
            and e.get("stage") == "endpoint"
            and str(e["resource"]).startswith(endpoint_prefix)
        )
    ]


def scope_failures(adapter: str, report: dict) -> list[str]:
    """Whole scopes that failed, as the report records them; empty means green."""
    module = load(adapter) if adapter in REPORTS else None
    if module is not None:
        return module.ADAPTER.scope_failures(report)
    if adapter in ("pxweb_v1", "pxweb_v2"):
        return _incomplete_scopes(report)
    if adapter == "entryscape":
        failures = _discovery(report)
        interrupted = report.get("interrupted") or ""
        if interrupted and "budget" not in interrupted:
            failures.append(f"run: {interrupted}")
        return failures
    raise ValueError(f"Unknown adapter: {adapter}")


# Counters that prove an item was read from the source during the run. Counters of
# retained, withheld, reused or removed items are not evidence of a working source.
READ_COUNTS = (
    "harvested",
    "rebuilt",
    "unchanged",
    "updated",
    "created",
    "checked",
    "relabelled",
)


def adapter_failed(adapter: str, report: dict, failures: list[str]) -> bool:
    """Whether the whole adapter failed, not only some of its scopes."""
    if not failures:
        return False
    if adapter in ("pxweb_v1", "pxweb_v2"):
        scopes = report.get("scopes") or []
        return len(failures) >= len(scopes)
    counts = report.get("counts") or {}
    return not any(counts.get(key) for key in READ_COUNTS)


def _warn(adapter: str, failures: list[str]) -> None:
    message = f"{adapter} completed with errors; failed scopes: {', '.join(failures)}"
    print(f"::warning title=Partial harvest::{message}")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as file:
            file.write(f"⚠️ {message}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", choices=sorted(REPORTS), required=True)
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    report = read(args.data / args.adapter / REPORTS[args.adapter])
    if (
        args.adapter == "pxweb_v1"
    ):  # scopes live in refresh.json, outcomes in harvest.json
        harvest = args.data / args.adapter / "reports/harvest.json"
        if harvest.exists():
            report = {**read(harvest), "scopes": report.get("scopes")}
    failures = scope_failures(args.adapter, report)
    failed = adapter_failed(args.adapter, report, failures)
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "counts": report.get("counts"),
                "errors": len(report.get("errors") or []),
                "scope_failures": failures,
                "adapter_failed": failed,
            },
            ensure_ascii=False,
        )
    )
    if failures and not failed:
        _warn(args.adapter, failures)
    raise SystemExit(failed)


if __name__ == "__main__":
    main()

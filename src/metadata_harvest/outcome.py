"""Decide whether a finished job should fail: only when a whole scope failed.

Individual table, codelist, indicator or dataset errors are reported in the
published reports and stay visible there; they never fail a job. A job fails
only when a whole provider, language scope or base API could not be read, or
when the catalog could not take an adapter's accepted collections at all.
"""

import argparse
import json
from pathlib import Path

from metadata_harvest.storage import read

REPORTS = {
    "pxweb_v1": "reports/refresh.json",
    "pxweb_v2": "reports/harvest.json",
    "kolada": "reports/latest.json",
    "uka": "reports/latest.json",
    "sdb": "reports/latest.json",
    "trafa": "reports/latest.json",
    "entrysoft": "reports/latest.json",
    "catalog": "reports/latest.json",
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
    if adapter in ("pxweb_v1", "pxweb_v2"):
        return _incomplete_scopes(report)
    if adapter == "kolada":
        return _discovery(report, resource=lambda r: r == "kpi")
    if adapter == "sdb":
        return _discovery(report, resource=lambda r: "/" not in r)
    if adapter == "trafa":
        return _scoped(report, discovery_resources=("products",), endpoint_prefix="*/")
    if adapter == "uka":
        return []  # the catalogue failing raises; indicator failures are individual
    if adapter == "entrysoft":
        failures = _discovery(report)
        interrupted = report.get("interrupted") or ""
        if interrupted and "budget" not in interrupted:
            failures.append(f"run: {interrupted}")
        return failures
    if adapter == "catalog":
        # Catalog-level errors: a missing or unreadable accepted collection.
        return [e["collection"] for e in report.get("errors") or []]
    raise ValueError(f"Unknown adapter: {adapter}")


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
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "counts": report.get("counts"),
                "errors": len(report.get("errors") or []),
                "scope_failures": failures,
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()

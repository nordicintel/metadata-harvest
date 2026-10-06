"""Verify complete Actions matrix evidence and print a compact summary."""

import json
import os
from pathlib import Path
from urllib.parse import unquote


def summarize(providers, reports):
    expected = {
        (p["code"], language, unquote(database))
        for p in providers
        for language in p["languages"]
        for database in p["database_ids"]
    }
    combinations = [item for report in reports for item in report["combinations"]]
    keys = [(x["provider"], x["language"], x["database_id"]) for x in combinations]
    passed = [
        x
        for x in combinations
        if x.get("equivalent")
        and all(x[form]["status"] == "success" for form in ("web", "api"))
    ]
    return {
        "expected_combinations": len(expected),
        "tested_combinations": len(combinations),
        "passed_combinations": len(passed),
        "records": sum(x["web"]["count"] for x in passed),
        "missing": sorted(expected - set(keys)),
        "unexpected": sorted(set(keys) - expected),
        "accepted": len(keys) == len(set(keys))
        and set(keys) == expected
        and len(reports) == len(providers)
        and all(r.get("accepted") for r in reports),
        "reports": reports,
    }


if __name__ == "__main__":
    providers = json.loads(
        Path("tests/fixtures/providers.json").read_text(encoding="utf-8")
    )
    reports = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in Path("output/live").glob("*.json")
    ]
    summary = summarize(providers, reports)
    Path("output/live-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = (
        f"Live PXWeb: {summary['passed_combinations']}/{summary['expected_combinations']} "
        f"combinations passed; {summary['records']} records. Accepted: {summary['accepted']}.\n"
    )
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)
    raise SystemExit(0 if summary["accepted"] else 1)

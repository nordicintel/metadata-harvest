"""Require complete, successful metadata coverage of the configured live matrix."""

import json
import os
from pathlib import Path
from urllib.parse import unquote

import registry
from apps.harvest.metadata_adapters.pxweb_v1.workflow import provider_row


def summarize(providers, reports):
    expected = {
        (p["code"], lang, unquote(db))
        for p in providers
        for lang in p["languages"]
        for db in p["database_ids"]
    }
    keys = [
        (r["provider"], c["language"], c["database_id"])
        for r in reports
        for c in r["combinations"]
    ]
    combinations = [c for r in reports for c in r["combinations"]]
    tables = [t for c in combinations for t in c["tables"]]
    passed = sum(t["status"] == "success" for t in tables)
    complete = all(
        c["status"] == "success" and len(c["tables"]) == c.get("web_count")
        for c in combinations
    )
    return {
        "expected_combinations": len(expected),
        "tested_combinations": len(keys),
        "missing": sorted(expected - set(keys)),
        "unexpected": sorted(set(keys) - expected),
        "tables": len(tables),
        "passed_tables": passed,
        "failed_tables": len(tables) - passed,
        "requests": sum(t.get("requests", 0) for t in tables),
        "accepted": set(keys) == expected
        and len(keys) == len(expected)
        and complete
        and passed == len(tables)
        and all(r.get("accepted") for r in reports),
        "reports": reports,
    }


if __name__ == "__main__":
    providers = [provider_row(b) for b in registry.load().bindings("pxweb_v1")]
    reports = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in Path("output/metadata-live").glob("*.json")
    ]
    summary = summarize(providers, reports)
    Path("output/metadata-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = f"Metadata: {summary['passed_tables']}/{summary['tables']} tables passed across {summary['tested_combinations']}/{summary['expected_combinations']} combinations. Accepted: {summary['accepted']}.\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)
    raise SystemExit(0 if summary["accepted"] else 1)

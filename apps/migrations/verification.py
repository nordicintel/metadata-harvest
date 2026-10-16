"""Prove that assembled accepted collections retain every audited source record."""

import argparse
import hashlib
import json
from pathlib import Path

from apps.migrations.collections import encoded
from apps.model.models import Dataset
from apps.parsing.accepted import read_records
from apps.parsing.contributions import legacy_document


def verify(data: Path, audit: Path, adapters=None):
    reports = {}
    for ledger in sorted(audit.glob("*-ledger.jsonl.gz")):
        adapter = ledger.name.removesuffix("-ledger.jsonl.gz")
        if adapters and adapter not in adapters:
            continue
        expected = {}
        for row in read_records(ledger):
            key = (row["dataset"], row["distribution"])
            if key in expected:
                raise ValueError(f"Duplicate source contribution: {key}")
            expected[key] = row["source_sha256"]
        seen = set()
        datasets = 0
        for path in sorted((data / adapter / "metadata").glob("*.jsonl.gz")):
            for record in read_records(path):
                dataset = Dataset.from_jsonld(record)
                datasets += 1
                for distribution in dataset.distributions:
                    key = (dataset.id, distribution.id)
                    if key in seen or key not in expected:
                        raise ValueError(
                            f"Unexpected or duplicated contribution: {key}"
                        )
                    restored = legacy_document(distribution, dataset)
                    digest = hashlib.sha256(encoded(restored).encode()).hexdigest()
                    if digest != expected[key]:
                        raise ValueError(f"Assembled source changed: {key}")
                    seen.add(key)
        if seen != set(expected):
            raise ValueError(
                f"Missing {len(set(expected) - seen)} contributions in {adapter}"
            )
        reports[adapter] = {
            "datasets": datasets,
            "verified_contributions": len(seen),
            "round_trip": "exact",
        }
        print(adapter, reports[adapter], flush=True)
    target = audit / "assembled-verification.json"
    prior = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    prior.update(reports)
    target.write_text(json.dumps(prior, indent=2) + "\n", encoding="utf-8")
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--adapter", action="append", dest="adapters")
    args = parser.parse_args()
    verify(args.data, args.audit, args.adapters)

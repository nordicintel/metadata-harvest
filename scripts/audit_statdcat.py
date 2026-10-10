"""Offline corpus round-trip audit (does not modify accepted inputs)."""

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path

from metadata_harvest.models import Dataset
from metadata_harvest.statdcat import convert_dataset, legacy_document


def differences(a, b, path=""):
    if type(a) is not type(b):
        yield path
    elif isinstance(a, dict):
        for key in a.keys() | b.keys():
            p = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in a or key not in b:
                yield p
            else:
                yield from differences(a[key], b[key], p)
    elif isinstance(a, list):
        if len(a) != len(b):
            yield path
        else:
            for i, (x, y) in enumerate(zip(a, b, strict=True)):
                yield from differences(x, y, path + "/" + str(i))
    elif a != b:
        yield path


def audit(data):
    counts = Counter()
    problems = []
    for path in sorted(data.glob("*/metadata/*.gz")):
        adapter = path.parent.parent.name
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                source = json.loads(line)
                try:
                    part = convert_dataset(source, adapter=adapter)
                    restored = Dataset.from_jsonld(part.dataset.to_jsonld())
                    projected = legacy_document(restored.distributions[0], restored)
                    diff = list(differences(source, projected))
                    if diff:
                        counts[adapter + ":changed"] += 1
                        if len(problems) < 150:
                            problems.append(
                                {
                                    "file": str(path),
                                    "line": number,
                                    "differences": diff[:30],
                                }
                            )
                    else:
                        counts[adapter + ":identical"] += 1
                except Exception as error:
                    counts[adapter + ":invalid"] += 1
                    if len(problems) < 150:
                        problems.append(
                            {
                                "file": str(path),
                                "line": number,
                                "error": str(error)[:1200],
                            }
                        )
        print(path.name, dict(counts), flush=True)
    return {"counts": dict(counts), "problems": problems}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("migration-audit.json"))
    args = parser.parse_args()
    result = audit(args.data)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if result["problems"]:
        raise SystemExit(1)

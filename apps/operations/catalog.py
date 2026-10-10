"""Command routing for offline catalog builds and explicit publication."""

import argparse
import json
from pathlib import Path

from apps.catalog.assembly import build
from apps.catalog.inputs import inputs
from apps.harvest.registry import ADAPTERS
from apps.paths import CONFIG
from apps.publication.archives import read
from apps.publication.candidates import promote, verify_published
from apps.publication.git import publish as publish_git


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "publish"])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("output/catalog"))
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    verify_published(args.data, [k for k, v in ADAPTERS.items() if v.available])
    if args.command == "build":
        result = build(args.data, args.output, config_dir=args.config)
    else:
        result = promote(args.data, args.output, config_dir=args.config)
        record = read(args.output / "result.json")

        def verify():
            if inputs(args.data, record["adapters"]) != record["inputs"]:
                raise ValueError("Stale catalog source; rebuild without harvesting")

        publish_git(
            "Publish current shared catalog", data=args.data / "catalog", verify=verify
        )
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()

"""Manual command routing for parsing source files into accepted collections."""

import argparse
import json
from pathlib import Path

from apps.configuration import (
    activate_configuration,
    load_configuration,
    use_configuration,
)
from apps.harvest.logs import configure as configure_logging
from apps.harvest.registry import ADAPTERS
from apps.parsing.build import apply, build
from apps.publication.git import publish


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "apply", "publish"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--adapter", choices=list(ADAPTERS))
    group.add_argument("--all", action="store_true")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("output/parsing"))
    parser.add_argument("--portal", type=Path, default=Path("data/data_eu"))
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    configure_logging()
    activate_configuration(load_configuration(args.config))
    adapters = (
        [name for name, spec in ADAPTERS.items() if spec.available]
        if args.all
        else [args.adapter]
    )
    results = {}
    for adapter in adapters:
        output = args.output / adapter
        if args.command == "build":
            results[adapter] = build(adapter, args.data, output, portal=args.portal)
        elif args.command == "apply":
            results[adapter] = apply(adapter, args.data, output)
        else:
            publish(
                f"Parse {adapter}: regenerate accepted collections",
                data=args.data / adapter,
            )
            results[adapter] = {"status": "published"}
    print(json.dumps(results, ensure_ascii=True))


def main():
    with use_configuration():
        _main()


if __name__ == "__main__":
    main()

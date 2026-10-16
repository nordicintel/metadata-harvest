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
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Apply even when fewer datasets parsed than the committed collections hold",
    )
    args = parser.parse_args()
    configure_logging()
    activate_configuration(load_configuration(args.config))
    adapters = (
        [name for name, spec in ADAPTERS.items() if spec.available]
        if args.all
        else [args.adapter]
    )
    results, failed = {}, False
    for adapter in adapters:
        output = args.output / adapter
        try:
            if args.command == "build":
                results[adapter] = build(adapter, args.data, output, portal=args.portal)
            elif args.command == "apply":
                results[adapter] = apply(
                    adapter, args.data, output, allow_shrink=args.allow_shrink
                )
            else:
                publish(
                    f"Parse {adapter}: regenerate accepted collections",
                    data=args.data / adapter,
                )
                results[adapter] = {"status": "published"}
        except Exception as error:  # report every adapter, then fail
            failed = True
            results[adapter] = {
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
    print(json.dumps(results, ensure_ascii=True))
    if failed:
        raise SystemExit(1)


def main():
    with use_configuration():
        _main()


if __name__ == "__main__":
    main()

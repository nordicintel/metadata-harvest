"""Command routing for portal import, refresh and explicit publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.portal.snapshot import import_snapshot, refresh_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["import", "refresh", "publish"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/data_eu"))
    args = parser.parse_args(argv)
    if args.command == "import":
        if args.source is None:
            parser.error("--source is required for import")
        result = import_snapshot(args.source, args.output)
    elif args.command == "refresh":
        result = refresh_snapshot(args.output)
    else:
        from apps.publication.git import publish

        publish("Refresh oppnadata editorial snapshot", data=args.output)
        result = {"status": "published"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()

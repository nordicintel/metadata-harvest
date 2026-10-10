"""Generate GitHub scheduling blocks from one config; never dispatch harvests."""

import argparse
import json
import os
import re
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from metadata_harvest.registry import ADAPTERS


def _cron(expression: str) -> None:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron requires five numeric POSIX fields")
    for value, (low, high) in zip(
        fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6)), strict=True
    ):
        for term in value.split(","):
            part, *steps = term.split("/")
            if len(steps) > 1 or (
                steps and (not steps[0].isdigit() or not 1 <= int(steps[0]) <= high + 1)
            ):
                raise ValueError(f"Invalid cron step: {term}")
            if part == "*":
                continue
            bounds = part.split("-")
            if (
                len(bounds) > 2
                or any(not x.isdigit() or not low <= int(x) <= high for x in bounds)
                or int(bounds[0]) > int(bounds[-1])
            ):
                raise ValueError(f"Invalid cron field: {term}")


def load(path: Path) -> dict:
    """Read and validate complete scheduling configuration, including enablement."""
    config = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    try:
        ZoneInfo(config["timezone"])
    except (KeyError, TypeError, ZoneInfoNotFoundError) as error:
        raise ValueError(
            "Invalid IANA schedule timezone; install harvest extras"
        ) from error
    expected = {k for k, v in ADAPTERS.items() if v.available}
    if set(config.get("adapters", {})) != expected:
        raise ValueError(
            "Schedule config must name every implemented adapter exactly once"
        )
    if set(config.get("snapshots", {})) - {"oppnadata"}:
        raise ValueError("Unknown snapshot schedule")
    for adapter, item in {**config["adapters"], **config.get("snapshots", {})}.items():
        if not isinstance(item.get("enabled"), bool) or not item.get("schedules"):
            raise ValueError("Each adapter requires enabled and nonempty schedules")
        seen = set()
        for schedule in item["schedules"]:
            cron, mode = schedule.get("cron"), schedule.get("mode")
            if (
                not isinstance(cron, str)
                or mode not in {"incremental", "full"}
                or (adapter == "kolada" and mode == "full")
            ):
                raise ValueError("Invalid scheduled mode or cron")
            _cron(cron)
            if cron in seen:
                raise ValueError("Duplicate schedule expression")
            seen.add(cron)
    return config


def resolve(config: dict, adapter: str, *, event: str, cron="", full=False) -> dict:
    """Manual selection is deliberate; scheduled mode comes from its exact entry."""
    item = config["adapters"][adapter]
    if event == "schedule":
        matches = [s for s in item["schedules"] if s["cron"] == cron]
        if len(matches) != 1:
            raise ValueError("Unconfigured schedule event")
        return {"enabled": item["enabled"], "full": matches[0]["mode"] == "full"}
    if event != "workflow_dispatch":
        raise ValueError("Unsupported harvesting event")
    if adapter == "kolada" and full:
        raise ValueError("Kolada does not support full refresh")
    return {"enabled": True, "full": bool(full)}


def _replace(text: str, name: str, body: str) -> str:
    pattern = rf"(?m)^( *)# BEGIN GENERATED {name}\n.*?^\1# END GENERATED {name}"
    match = re.search(pattern, text, flags=re.S)
    if not match:
        raise ValueError(f"Missing generated block: {name}")
    indent = match[1]
    replacement = (
        indent
        + f"# BEGIN GENERATED {name}\n"
        + "\n".join(indent + line for line in body.splitlines())
        + "\n"
        + indent
        + f"# END GENERATED {name}"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def sync(root: Path, *, check=False) -> list[str]:
    """Update only marked scheduling sections; --check never writes files."""
    root = Path(root)
    config = load(root / "schedules.toml")
    changed = []
    for adapter, item in {**config["adapters"], **config.get("snapshots", {})}.items():
        filename = (
            "refresh-portal.yml"
            if adapter == "oppnadata"
            else ADAPTERS[adapter].workflow_file
        )
        path = root / ".github/workflows" / filename
        original = path.read_text(encoding="utf-8")
        body = "schedule:\n" + "\n".join(
            f"  - cron: '{s['cron']}'\n    timezone: {config['timezone']}"
            for s in item["schedules"]
        )
        text = _replace(original, "SCHEDULE", body)
        text = _replace(
            text,
            "ENABLEMENT",
            f"if: github.event_name == 'workflow_dispatch' || {str(item['enabled']).lower()}",
        )
        if text != original:
            changed.append(path.name)
            if not check:
                path.write_text(text, encoding="utf-8", newline="\n")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sync", "resolve"])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--adapter", choices=list(ADAPTERS))
    args = parser.parse_args()
    if args.command == "sync":
        changed = sync(args.root, check=args.check)
        print(json.dumps({"outdated" if args.check else "updated": changed}))
        if args.check and changed:
            raise SystemExit(1)
    else:
        result = resolve(
            load(args.root / "schedules.toml"),
            args.adapter,
            event=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"),
            cron=os.environ.get("SCHEDULE_CRON", ""),
            full=os.environ.get("MANUAL_FULL", "").lower() == "true",
        )
        line = f"FULL_REFRESH={str(result['full']).lower()}\n"
        if os.environ.get("GITHUB_ENV"):
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
                stream.write(line)
        print(line.strip())


if __name__ == "__main__":
    main()

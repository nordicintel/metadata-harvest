"""GitHub Actions and local entry point for the persistent harvesting workflow."""

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path

from metadata_harvest.adapters.pxweb_v2 import workflow as v2_workflow
from metadata_harvest.artifacts import check_collection_sizes, publication_directory
from metadata_harvest.registry import ADAPTERS, get_adapter
from metadata_harvest.storage import read
from metadata_harvest.workflow import (
    build_provider,
    collect_metadata,
    collect_refresh,
    prepare,
    refresh_provider,
)


def outputs(values):
    lines = [
        f"{key}={json.dumps(value, separators=(',', ':')) if not isinstance(value, str) else value}"
        for key, value in values.items()
    ]
    print("\n".join(lines), flush=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def publish(message, *, data=Path("data/pxweb_v1")):
    """Commit one adapter only; replay over unrelated pushes without force."""

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], check=check, text=True, capture_output=True
        )

    root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    relative = publication_directory(root, Path(data))
    check_collection_sizes(data)
    staged = git("diff", "--cached", "--name-only", "-z").stdout.split("\0")
    if any(path and not path.startswith(relative + "/") for path in staged):
        raise RuntimeError("Refusing to publish unrelated staged files")
    git("add", "--", relative)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-m", message)
    for attempt in range(3):
        result = git("push", "origin", "HEAD:main", check=False)
        if result.returncode == 0:
            return
        if attempt < 2:
            git("fetch", "origin", "main")
            if git("rebase", "origin/main", check=False).returncode:
                git("rebase", "--abort", check=False)
                raise RuntimeError(
                    "Publishing conflicted with a newer change; no force push was attempted"
                )
    raise RuntimeError(f"Could not publish harvest results: {result.stderr}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "prepare",
            "refresh",
            "collect-refresh",
            "build",
            "collect-metadata",
            "publish",
            "continue",
            "run",
            "collect",
        ],
    )
    parser.add_argument("--adapter", choices=list(ADAPTERS), default="pxweb_v1")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--providers", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--run-id")
    parser.add_argument("--wave", type=int, default=0)
    parser.add_argument("--artifacts", type=Path, default=Path("output/artifacts"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--budget-seconds", type=float, default=19_200)
    args = parser.parse_args()
    spec = get_adapter(args.adapter)
    if args.command not in spec.commands:
        parser.error(f"{args.adapter} supports {spec.commands}")
    is_v2 = args.adapter == "pxweb_v2"
    args.data = args.data or Path("data") / args.adapter
    args.providers = args.providers or Path(spec.configuration)
    args.output = args.output or Path("output/pxweb_v2" if is_v2 else "output/provider")
    providers = read(args.providers)
    if is_v2 and args.command not in {"run", "collect", "publish", "continue"}:
        parser.error("PXWeb v2 uses run, collect, publish and continue")
    if args.command in {"run", "collect"}:
        if not is_v2 or len(providers) != 1:
            parser.error("run/collect require the SCB-only pxweb_v2 configuration")
        if args.command == "run":
            expected = os.environ.get("EXPECTED_RUN_ID")
            checkpoint = args.data / "work/current.json"
            if expected and (
                not checkpoint.exists() or read(checkpoint)["run_id"] != expected
            ):
                outputs(
                    {"skip": "true", "reason": "Continuation was already completed"}
                )
                return
            result = asyncio.run(
                v2_workflow.run(
                    providers[0],
                    args.data,
                    args.output,
                    full=args.full,
                    budget_seconds=args.budget_seconds,
                )
            )
        else:
            artifact = read(args.output / "result.json")
            result = v2_workflow.collect(
                args.data,
                args.output,
                args.run_id or artifact["run_id"],
                args.wave or artifact["wave"],
            )
        outputs(
            {k: result[k] for k in ("run_id", "wave", "status", "continue", "counts")}
        )
        return
    if args.command == "prepare":
        plan = prepare(args.data, providers)
        if (
            os.environ.get("EXPECTED_RUN_ID")
            and plan["run_id"] != os.environ["EXPECTED_RUN_ID"]
        ):
            outputs({"skip": "true", "reason": "Continuation was already completed"})
        else:
            outputs({**plan, "skip": "false"})
    elif args.command in {"refresh", "build"}:
        provider = next((p for p in providers if p["code"] == args.provider), None)
        if provider is None or not args.run_id:
            parser.error("A configured provider and run ID are required")
        if args.command == "refresh":
            asyncio.run(refresh_provider(provider, args.run_id, args.output))
        else:
            asyncio.run(
                build_provider(
                    args.data,
                    provider,
                    args.run_id,
                    args.wave,
                    args.output,
                    budget_seconds=args.budget_seconds,
                )
            )
    elif args.command == "collect-refresh":
        state = collect_refresh(
            args.data,
            providers,
            [read(p) for p in args.artifacts.rglob("result.json")],
            args.run_id,
            full=args.full,
        )
        outputs(
            {
                "providers": sorted({task["provider_code"] for task in state["tasks"]}),
                "wave": state["wave"],
            }
        )
    elif args.command == "collect-metadata":
        result = collect_metadata(
            args.data, list(args.artifacts.rglob("result.json")), args.run_id, args.wave
        )
        outputs(result)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(
                os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8"
            ) as stream:
                stream.write(
                    f"Harvest **{result['status']}**\n\n"
                    + "\n".join(
                        f"- {key}: {value}" for key, value in result["counts"].items()
                    )
                    + "\n"
                )
    elif args.command == "publish":
        publish(
            f"Harvest {args.adapter} {args.run_id}: persist verified records and outcomes",
            data=args.data,
        )
    elif args.command == "continue":
        if not args.run_id:
            parser.error("Continuation requires a run ID")
        subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                spec.workflow_file,
                "--ref",
                "main",
                "-f",
                f"resume_run={args.run_id}",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()

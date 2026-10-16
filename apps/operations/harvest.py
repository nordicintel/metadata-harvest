"""GitHub Actions and local entry point for the persistent harvesting workflow."""

import argparse
import asyncio
import importlib
import json
import os
import subprocess
from pathlib import Path

import registry
from apps.harvest.logs import configure as configure_logging
from apps.harvest.metadata_adapters import ADAPTERS, load, workflow_file
from apps.harvest.metadata_adapters.pxweb_v1.workflow import (
    build_provider,
    collect_metadata,
    collect_refresh,
    prepare,
    provider_row,
    refresh_provider,
)
from apps.harvest.metadata_adapters.pxweb_v2 import workflow as v2_workflow
from apps.harvest.storage import read
from apps.publication.git import publish
from harvest import lifecycle

PXWEB_V1_COMMANDS = (
    "prepare",
    "refresh",
    "collect-refresh",
    "build",
    "collect-metadata",
    "publish",
    "continue",
)


def _entryscape_options(args):
    options = {"selected": args.provider_code}
    if args.max_requests:
        options["max_requests"] = args.max_requests
    return options


# Single-worker adapters sharing the run/collect/publish lifecycle. Each entry maps
# the adapter name to: whether run() takes the first configured binding or the
# whole list, the run() keyword arguments taken from the parsed CLI arguments, and
# the collect() keyword arguments taken from the first configured binding.
SIMPLE_ADAPTERS = {
    "entryscape": ("all", _entryscape_options, lambda b: {}),
    "forsakringskassan": (
        "single",
        lambda a: {"subjects": a.subject, "languages": a.language, "full": a.full},
        lambda b: {"languages": b.options["languages"]},
    ),
    "dataanalys": (
        "single",
        lambda a: {
            "areas": a.subject,
            "indicators": a.indicator,
            "languages": a.language,
        },
        lambda b: {"languages": b.options["languages"]},
    ),
}


def run_adapter(args, adapter):
    """Run or collect one adapter through the shared lifecycle."""
    args.output = args.output or Path("output") / adapter.name
    if args.command == "run":
        selection = {
            name: getattr(args, flag.replace("-", "_"))
            for flag, name in adapter.options.items()
        }
        selection["languages"] = args.language
        binding = args.registry.bindings(args.adapter)[0]
        result = asyncio.run(
            lifecycle.run(adapter, binding, args.data, args.output, selection=selection)
        )
    else:
        result = lifecycle.collect(adapter, args.data, args.output)
    outputs({k: result[k] for k in ("run_id", "status", "continue", "counts")})


def run_simple(args):
    """Run or collect one single-worker adapter and print its outcome."""
    bindings_argument, run_options, collect_options = SIMPLE_ADAPTERS[args.adapter]
    workflow = importlib.import_module(
        f"apps.harvest.metadata_adapters.{args.adapter}.workflow"
    )
    args.output = args.output or Path("output") / args.adapter
    bindings = args.registry.bindings(args.adapter)
    if args.command == "run":
        binding = bindings if bindings_argument == "all" else bindings[0]
        result = asyncio.run(
            workflow.run(binding, args.data, args.output, **run_options(args))
        )
    else:
        result = workflow.collect(
            args.data, args.output, **collect_options(bindings[0])
        )
    outputs({k: result[k] for k in ("run_id", "status", "continue", "counts")})


def outputs(values):
    lines = [
        f"{key}={json.dumps(value, separators=(',', ':')) if not isinstance(value, str) else value}"
        for key, value in values.items()
    ]
    print("\n".join(lines), flush=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=[*PXWEB_V1_COMMANDS, "run", "collect"])
    parser.add_argument("--adapter", choices=list(ADAPTERS), default="pxweb_v1")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--kpi", nargs="+")
    parser.add_argument("--scope", choices=("municipality", "ou"))
    parser.add_argument("--indicator", nargs="+")
    parser.add_argument("--subject", nargs="+")
    parser.add_argument("--product", nargs="+")
    parser.add_argument("--provider-code", nargs="+")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--language", nargs="+", choices=("sv", "en"))
    parser.add_argument("--data", type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        help="Directory containing the three source configuration files",
    )
    parser.add_argument("--provider")
    parser.add_argument("--run-id")
    parser.add_argument("--wave", type=int, default=0)
    parser.add_argument("--artifacts", type=Path, default=Path("output/artifacts"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--budget-seconds", type=float, default=19_200)
    args = parser.parse_args()
    configure_logging()
    is_v1 = args.adapter == "pxweb_v1"
    commands = PXWEB_V1_COMMANDS if is_v1 else ("run", "collect", "publish")
    if args.command not in commands:
        parser.error(f"{args.adapter} supports {commands}")
    if args.full and args.adapter in {
        "smhi_emissions",
        "riksbank",
        "dataanalys",
        "tillvaxtverket",
    }:
        parser.error(f"{args.adapter} does not support --full; use its normal refresh")
    is_v2 = args.adapter == "pxweb_v2"
    args.data = args.data or Path("data") / args.adapter
    args.registry = registry.load(args.config)
    if args.adapter == "kolada":
        from apps.harvest.metadata_adapters.kolada import workflow as kolada_workflow

        if args.full:
            parser.error("Kolada uses targeted incremental refreshes, not --full")
        if args.command in {"run", "collect"}:
            args.output = args.output or Path("output/kolada")
            if args.command == "run":
                result = asyncio.run(
                    kolada_workflow.run(
                        args.registry.bindings("kolada")[0],
                        args.data,
                        args.output,
                        kpis=args.kpi,
                        scope=args.scope,
                        budget_seconds=args.budget_seconds,
                    )
                )
            else:
                result = kolada_workflow.collect(args.data, args.output)
            outputs({k: result[k] for k in ("run_id", "status", "counts")})
            return
    module = load(args.adapter)
    if module is not None and args.command in {"run", "collect"}:
        run_adapter(args, module.ADAPTER)
        return
    if args.adapter in SIMPLE_ADAPTERS and args.command in {"run", "collect"}:
        run_simple(args)
        return
    args.output = args.output or Path("output/pxweb_v2" if is_v2 else "output/provider")
    bindings = args.registry.bindings(args.adapter)
    if is_v2 and args.command not in {"run", "collect", "publish"}:
        parser.error("PXWeb v2 uses run, collect and publish")
    if args.command in {"run", "collect"}:
        if not is_v2 or len(bindings) != 1:
            parser.error("run/collect require the SCB-only pxweb_v2 configuration")
        if args.command == "run":
            result = asyncio.run(
                v2_workflow.run(
                    bindings[0],
                    args.data,
                    args.output,
                    full=args.full,
                    budget_seconds=args.budget_seconds,
                )
            )
        else:
            artifact = read(args.output / "result.json")
            result = v2_workflow.collect(
                args.data, args.output, args.run_id or artifact["run_id"]
            )
        outputs({k: result[k] for k in ("run_id", "status", "counts")})
        return
    providers = [provider_row(binding) for binding in bindings]
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
                workflow_file("pxweb_v1"),
                "--ref",
                "main",
                "-f",
                f"resume_run={args.run_id}",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()

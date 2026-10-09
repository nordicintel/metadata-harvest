"""Offline catalog construction from accepted adapter collections only.

No harvesting imports, source requests or workflow dispatch are permitted here.
"""

import argparse
import gzip
import json
import shutil
import subprocess
from pathlib import Path

from metadata_harvest.artifacts import check_collection_sizes, manifest
from metadata_harvest.processing import content_enrichment, semantic_enrichment
from metadata_harvest.publication import publish as publish_git
from metadata_harvest.registry import ADAPTERS


def read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def inputs(data: Path, adapters: list[str]) -> dict:
    """Fingerprint only accepted collections and latest reports, never work/listings."""
    result = {}
    for adapter in adapters:
        for directory in ("metadata", "codelists", "reports"):
            root = data / adapter / directory
            for relative, digest in manifest(root).items():
                if (
                    directory == "reports" and relative.endswith(".json")
                ) or relative.endswith(".jsonl.gz"):
                    result[f"{adapter}/{directory}/{relative}"] = digest
    return result


def _check_gzip(path: Path) -> None:
    # Container integrity only: do not parse or schema-validate trusted datasets.
    with gzip.open(path, "rb") as stream:
        while stream.read(1024 * 1024):
            pass


def build(data: Path, output: Path, *, adapters=None) -> dict:
    """Copy current trusted collections; errors retain previous catalog collections."""
    data, output = Path(data), Path(output)
    catalog = data / "catalog"
    if (
        output.resolve() == data.resolve()
        or data.resolve() in output.resolve().parents
        or output.resolve() in data.resolve().parents
    ):
        raise ValueError("Candidate must be separate from source data")
    if content_enrichment.CONFIGURED or semantic_enrichment.CONFIGURED:
        raise NotImplementedError(
            "Configured processors require an explicit catalog processing implementation"
        )
    adapters = sorted(
        adapters
        if adapters is not None
        else [k for k, v in ADAPTERS.items() if v.available]
    )
    if any(k not in ADAPTERS or not ADAPTERS[k].available for k in adapters):
        raise ValueError("Unknown or unavailable catalog adapter")
    baseline = inputs(data, adapters)
    catalog_baseline = manifest(catalog)
    previous = read(catalog / "state/current.json", {"accepted": {}})
    accepted = dict(previous["accepted"])
    errors, upstream = [], {}
    counts = {"copied": 0, "reused": 0, "retained": 0}
    output.mkdir(parents=True, exist_ok=True)
    # Reuse a dedicated candidate safely: remove only files from its prior manifest.
    old_candidate = read(output / "result.json", {})
    for name in old_candidate.get("candidate", {}):
        target = (output / name).resolve()
        if not target.is_relative_to(output.resolve()):
            raise ValueError("Unsafe candidate path")
        target.unlink(missing_ok=True)
    for name in sorted(set(accepted) | {k for k in baseline if "/reports/" not in k}):
        source, target, old = data / name, output / name, catalog / name
        try:
            if name not in baseline:
                raise ValueError(
                    "Missing accepted input collection; absence is not confirmed removal"
                )
            if (
                baseline[name] == accepted.get(name)
                and old.exists()
                and catalog_baseline.get(name) == baseline[name]
            ):
                counts["reused"] += 1
            else:
                _check_gzip(source)
                counts["copied"] += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            accepted[name] = baseline[name]
        except Exception as error:
            errors.append(
                {"collection": name, "error": f"{type(error).__name__}: {error}"}
            )
            if old.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(old, target)
                counts["retained"] += 1
    for adapter in adapters:
        upstream[adapter] = {}
        if not any(k.startswith(f"{adapter}/metadata/") for k in baseline):
            errors.append(
                {
                    "collection": adapter,
                    "error": "Missing accepted metadata collections",
                }
            )
        reports = [k for k in baseline if k.startswith(f"{adapter}/reports/")]
        if not reports:
            upstream[adapter]["status"] = "unknown"
        for name in reports:
            try:
                upstream[adapter][Path(name).name] = read(data / name)
            except Exception as error:
                errors.append({"collection": name, "error": str(error)})
    upstream_errors = any(
        isinstance(report, dict)
        and (report.get("errors") or report.get("status") not in (None, "success"))
        for reports in upstream.values()
        for report in reports.values()
    )
    result = {
        "status": "completed_with_errors" if errors or upstream_errors else "success",
        "counts": counts,
        "errors": errors,
        "upstream": upstream,
        "stages": {
            "content_enrichment": "not_configured",
            "semantic_enrichment": "not_configured",
        },
    }
    write(output / "state/current.json", {"accepted": accepted})
    write(output / "reports/latest.json", result)
    check_collection_sizes(output)
    write(
        output / "result.json",
        {
            "inputs": baseline,
            "adapters": adapters,
            "catalog_baseline": catalog_baseline,
            "candidate": manifest(output),
        },
    )
    return result


def promote(data: Path, output: Path) -> dict:
    """Verify candidate and source fingerprints before replacing catalog files."""
    data, output = Path(data), Path(output)
    record = read(output / "result.json")
    if (
        inputs(data, record["adapters"]) != record["inputs"]
        or manifest(data / "catalog") != record["catalog_baseline"]
    ):
        raise ValueError("Stale catalog candidate; rebuild from published files")
    if manifest(output) != record["candidate"]:
        raise ValueError("Candidate integrity mismatch")
    for name in record["candidate"]:
        target = (data / "catalog" / name).resolve()
        if not target.is_relative_to((data / "catalog").resolve()):
            raise ValueError("Unsafe catalog candidate path")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(output / name, temporary)
        temporary.replace(target)
    return read(output / "reports/latest.json")


def verify_published(data: Path, adapters: list[str]) -> None:
    """CLI inputs must be committed accepted results, not local worker changes."""
    paths = [
        str(data / adapter / kind)
        for adapter in adapters
        for kind in ("metadata", "codelists", "reports")
    ]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise ValueError(
            "Catalog requires committed adapter results; publish those deliberately first"
        )
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve()
    tracked = set(
        subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *paths],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\0")
    )
    for name in inputs(data, adapters):
        source = (data / name).resolve()
        if (
            not source.is_relative_to(root)
            or source.relative_to(root).as_posix() not in tracked
        ):
            raise ValueError("Catalog input is not committed in HEAD: " + str(source))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "publish"])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("output/catalog"))
    args = parser.parse_args()
    verify_published(args.data, [k for k, v in ADAPTERS.items() if v.available])
    if args.command == "build":
        result = build(args.data, args.output)
    else:
        result = promote(args.data, args.output)
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

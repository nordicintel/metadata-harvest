"""Offline catalog construction from accepted adapter collections only.

No harvesting imports, source requests or workflow dispatch are permitted here.
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from metadata_harvest.artifacts import check_collection_sizes, manifest
from metadata_harvest.processing import content_enrichment, semantic_enrichment
from metadata_harvest.publication import publish as publish_git
from metadata_harvest.registry import ADAPTERS

ASSETS = ("catalog.7z", "codelists.7z")
CATALOG_FILES = {*ASSETS, "state/current.json", "reports/latest.json"}


def sevenzip() -> str:
    """Find the native 7-Zip command on Linux, macOS or Windows."""
    for name in ("7zz", "7z"):
        if executable := shutil.which(name):
            return executable
    if os.name == "nt":
        executable = (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip/7z.exe"
        )
        if executable.is_file():
            return str(executable)
    raise FileNotFoundError("Catalog construction requires 7-Zip (7zz or 7z)")


def _extract(archive: Path, target: Path, member: str) -> None:
    # Stream one explicitly named file; never extract archive paths onto disk.
    with target.open("wb") as stream:
        subprocess.run(
            [sevenzip(), "e", "-so", str(archive), member], stdout=stream, check=True
        )


def _compress(source: Path, archive: Path) -> None:
    """Create a deterministic LZMA2 archive without filesystem timestamps."""
    temporary = archive.with_suffix(".7z.tmp")
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                sevenzip(),
                "a",
                "-t7z",
                "-m0=lzma2",
                "-mx=7",
                "-md=64m",
                "-mmt=2",
                "-mtc=off",
                "-mta=off",
                "-mtm=off",
                str(temporary.resolve()),
                source.name,
            ],
            cwd=source.parent,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)


def _append(source: Path, target, *, member: dict | None = None) -> dict:
    """Append gzip JSONL or a verified range retained from a previous archive."""
    offset = target.tell()
    digest = hashlib.sha256()
    last = b""
    with source.open("rb") if member is not None else gzip.open(source, "rb") as stream:
        if member is not None:
            stream.seek(member["offset"])
        remaining = member["size"] if member is not None else None
        while remaining is None or remaining:
            chunk = stream.read(
                1024 * 1024 if remaining is None else min(remaining, 1024 * 1024)
            )
            if not chunk:
                if remaining:
                    raise ValueError(f"Truncated previous collection: {source}")
                break
            digest.update(chunk)
            target.write(chunk)
            last = chunk[-1:]
            if remaining is not None:
                remaining -= len(chunk)
    if last not in (b"", b"\n"):
        raise ValueError("Accepted JSONL collection must end with a newline")
    if member is not None and digest.hexdigest() != member["sha256"]:
        raise ValueError(f"Previous collection checksum mismatch: {source}")
    return {
        "offset": offset,
        "size": target.tell() - offset,
        "sha256": digest.hexdigest(),
    }


def _archives(
    catalog: Path, output: Path, selected: dict, accepted: dict, previous: dict
) -> dict:
    """Rebuild only changed archives, retaining failed collection byte ranges."""
    members = {}
    for kind, asset in (("metadata", "catalog.7z"), ("codelists", "codelists.7z")):
        names = sorted(name for name in selected if f"/{kind}/" in name)
        prior = {
            name: digest
            for name, digest in previous.get("accepted", {}).items()
            if f"/{kind}/" in name
        }
        if (
            asset in previous.get("assets", {})
            and prior == {name: accepted[name] for name in names}
            and all(name in previous.get("members", {}) for name in names)
        ):
            shutil.copyfile(catalog / asset, output / asset)
            members.update({name: previous["members"][name] for name in names})
            continue
        with TemporaryDirectory(prefix=".archive-", dir=output) as temporary:
            directory = Path(temporary)
            content = directory / (Path(asset).stem + ".jsonl")
            fallback = directory / "previous.jsonl"
            with content.open("wb") as target:
                for name in names:
                    source, member = selected[name]
                    if member is not None:
                        if not fallback.exists():
                            _extract(catalog / asset, fallback, content.name)
                        source = fallback
                    members[name] = _append(source, target, member=member)
            _compress(content, output / asset)
    return members


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _collection_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (
        len(parts) < 3
        or parts[0] not in ADAPTERS
        or parts[1] not in {"metadata", "codelists"}
        or ".." in parts
        or "\\" in name
        or not name.endswith(".jsonl.gz")
    ):
        raise ValueError(f"Unsafe collection path: {name}")
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Unsafe collection path: {name}")
    return target


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
    last = b""
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            last = chunk[-1:]
    if last not in (b"", b"\n"):
        raise ValueError("Accepted JSONL collection must end with a newline")


def build_legacy(data: Path, output: Path, *, adapters=None) -> dict:
    """Build two 7z archives; failed inputs retain their previous accepted records."""
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
    # Prior archives must match committed state before supplying retained records.
    for name, digest in previous.get("assets", {}).items():
        if name not in ASSETS:
            raise ValueError("Unexpected previous catalog asset")
        if not (catalog / name).exists() or _digest(catalog / name) != digest:
            raise ValueError(f"Missing or corrupt previous catalog asset: {name}")
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
    (output / "result.json").unlink(missing_ok=True)
    selected = {}
    for name in sorted(set(accepted) | {k for k in baseline if "/reports/" not in k}):
        source = _collection_path(data, name)
        old = _collection_path(catalog, name)
        try:
            if name not in baseline:
                raise ValueError(
                    "Missing accepted input collection; absence is not confirmed removal"
                )
            if baseline[name] == accepted.get(name):
                if name not in previous.get("members", {}):
                    _check_gzip(source)  # Check framing once during legacy migration.
                counts["reused"] += 1
            else:
                _check_gzip(source)
                counts["copied"] += 1
            selected[name] = (source, None)
            accepted[name] = baseline[name]
        except Exception as error:
            errors.append(
                {"collection": name, "error": f"{type(error).__name__}: {error}"}
            )
            if member := previous.get("members", {}).get(name):
                if member["offset"] < 0 or member["size"] < 0:
                    raise ValueError(
                        f"Invalid previous catalog member: {name}"
                    ) from error
                selected[name] = (None, member)
                counts["retained"] += 1
            elif old.exists() and _digest(old) == accepted.get(name):
                _check_gzip(old)
                selected[name] = (old, None)
                counts["retained"] += 1
            elif name in accepted:
                raise ValueError(
                    f"Missing or corrupt previous collection: {name}"
                ) from error
    members = _archives(catalog, output, selected, accepted, previous)
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
        "counts": {"collections": len(selected), "retained": counts["retained"]},
        "errors": errors,
        "upstream": upstream,
        "stages": {
            "content_enrichment": "not_configured",
            "semantic_enrichment": "not_configured",
        },
    }
    assets = {name: _digest(output / name) for name in ASSETS}
    write(
        output / "state/current.json",
        {
            "accepted": accepted,
            "members": members,
            "assets": assets,
        },
    )
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
    return {**result, "build_counts": counts}


def build(data: Path, output: Path, *, adapters=None) -> dict:
    from metadata_harvest.catalog_assembly import build as assemble_catalog

    return assemble_catalog(data, output, adapters=adapters)


def verify_candidate(data: Path, output: Path) -> dict:
    """Check all paths and fingerprints before any catalog mutation."""
    data, output = Path(data), Path(output)
    record = read(output / "result.json")
    if "dependencies" in record:
        from metadata_harvest.catalog_assembly import dependencies

        if dependencies(data) != record["dependencies"]:
            raise ValueError("Stale catalog dependencies; rebuild")
    if (
        inputs(data, record["adapters"]) != record["inputs"]
        or manifest(data / "catalog") != record["catalog_baseline"]
    ):
        raise ValueError("Stale catalog candidate; rebuild from published files")
    if manifest(output) != record["candidate"]:
        raise ValueError("Candidate integrity mismatch")
    if set(record["candidate"]) != CATALOG_FILES:
        raise ValueError("Unexpected catalog candidate files")
    return record


def promote(data: Path, output: Path) -> dict:
    """Promote assets locally and remove only verified legacy catalog collections."""
    data, output = Path(data), Path(output)
    record = verify_candidate(data, output)
    legacy = [
        _collection_path(data / "catalog", name)
        for name in record["catalog_baseline"]
        if name not in CATALOG_FILES
    ]
    for name in record["candidate"]:
        target = (data / "catalog" / name).resolve()
        if not target.is_relative_to((data / "catalog").resolve()):
            raise ValueError("Unsafe catalog candidate path")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(output / name, temporary)
        temporary.replace(target)
    for path in legacy:
        path.unlink()
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

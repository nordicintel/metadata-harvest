"""Offline catalog construction from accepted adapter collections only.

No harvesting imports, source requests or workflow dispatch are permitted here.
"""

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from metadata_harvest.artifacts import check_collection_sizes, manifest
from metadata_harvest.processing import content_enrichment, semantic_enrichment
from metadata_harvest.publication import publish as publish_git
from metadata_harvest.registry import ADAPTERS

ASSETS = ("catalog.jsonl.gz", "codelists.jsonl.gz")
CATALOG_FILES = {*ASSETS, "state/current.json", "reports/latest.json"}


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


def _copy_member(path: Path, target, *, offset: int, size: int, digest: str) -> None:
    """Copy one accepted compressed range, verifying its exact source bytes."""
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        source.seek(offset)
        remaining = size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"Truncated collection: {path}")
            checksum.update(chunk)
            target.write(chunk)
            remaining -= len(chunk)
    if checksum.hexdigest() != digest:
        raise ValueError(f"Collection changed while building: {path}")


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


def build(data: Path, output: Path, *, adapters=None) -> dict:
    """Combine accepted gzip members; failed inputs retain their previous members."""
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
    # A downloaded release must match committed state before supplying fallback data.
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
            selected[name] = (source, 0, source.stat().st_size, baseline[name])
            accepted[name] = baseline[name]
        except Exception as error:
            errors.append(
                {"collection": name, "error": f"{type(error).__name__}: {error}"}
            )
            if member := previous.get("members", {}).get(name):
                asset = (
                    "codelists.jsonl.gz"
                    if "/codelists/" in name
                    else "catalog.jsonl.gz"
                )
                if member["offset"] < 0 or member["size"] <= 0:
                    raise ValueError(
                        f"Invalid previous catalog member: {name}"
                    ) from error
                selected[name] = (
                    catalog / asset,
                    member["offset"],
                    member["size"],
                    accepted[name],
                )
                counts["retained"] += 1
            elif old.exists() and _digest(old) == accepted.get(name):
                _check_gzip(old)
                selected[name] = (old, 0, old.stat().st_size, accepted[name])
                counts["retained"] += 1
            elif name in accepted:
                raise ValueError(
                    f"Missing or corrupt previous collection: {name}"
                ) from error
    members = {}
    with ExitStack() as stack:
        streams = {
            name: stack.enter_context((output / name).open("wb")) for name in ASSETS
        }
        for name, (source, offset, size, digest) in selected.items():
            asset = (
                "codelists.jsonl.gz" if "/codelists/" in name else "catalog.jsonl.gz"
            )
            target = streams[asset]
            members[name] = {"offset": target.tell(), "size": size}
            _copy_member(source, target, offset=offset, size=size, digest=digest)
        for stream in streams.values():
            if stream.tell() == 0:
                stream.write(gzip.compress(b"", mtime=0))
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
    release_digest = hashlib.sha256(
        json.dumps([assets, accepted, members, result], sort_keys=True).encode()
    ).hexdigest()
    write(
        output / "state/current.json",
        {
            "accepted": accepted,
            "members": members,
            "assets": assets,
            "release": f"catalog-{release_digest[:24]}",
        },
    )
    write(output / "reports/latest.json", result)
    check_collection_sizes(output / "state")
    check_collection_sizes(output / "reports")
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


def verify_candidate(data: Path, output: Path) -> dict:
    """Check all paths and fingerprints before any catalog mutation."""
    data, output = Path(data), Path(output)
    record = read(output / "result.json")
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


def restore_assets(data: Path) -> None:
    """Restore the release pinned by committed state, checking both assets first."""
    catalog = Path(data) / "catalog"
    state = read(catalog / "state/current.json", {})
    if not state.get("release"):
        return  # Initial migration uses existing grouped catalog files.
    expected = state["assets"]
    if set(expected) != set(ASSETS):
        raise ValueError("Unexpected catalog release assets")
    if all(
        (catalog / name).exists() and _digest(catalog / name) == digest
        for name, digest in expected.items()
    ):
        return
    with TemporaryDirectory(prefix=".release-", dir=catalog) as temporary:
        directory = Path(temporary)
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                state["release"],
                "--pattern",
                ASSETS[0],
                "--pattern",
                ASSETS[1],
                "--dir",
                str(directory),
            ],
            check=True,
        )
        for name, digest in expected.items():
            if not (directory / name).exists() or _digest(directory / name) != digest:
                raise ValueError(f"Downloaded catalog asset checksum mismatch: {name}")
        for name in expected:
            (directory / name).replace(catalog / name)


def _release(tag: str) -> dict | None:
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/releases/tags/{tag}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if "(HTTP 404)" in result.stderr:
            # Drafts may have no Git tag yet and are absent from the tag endpoint.
            listing = subprocess.run(
                [
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    "repos/{owner}/{repo}/releases?per_page=100",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return next(
                (
                    release
                    for page in json.loads(listing.stdout)
                    for release in page
                    if release["tag_name"] == tag
                ),
                None,
            )
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )
    return json.loads(result.stdout)


def publish_release(data: Path, output: Path) -> str:
    """Upload and verify a complete draft before making the release downloadable.

    Content-addressed releases allow interrupted uploads to be retried without
    overwriting assets in an already published release. Git state is promoted only
    after this succeeds; its pinned tag remains the recovery source on failure.
    """
    data, output = Path(data), Path(output)
    verify_candidate(data, output)
    state = read(output / "state/current.json")
    tag = state["release"]
    files = [output / name for name in ASSETS] + [
        output / "state/current.json",
        output / "reports/latest.json",
    ]
    expected = {path.name: "sha256:" + _digest(path) for path in files}
    release = _release(tag)
    if release is None:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--draft",
                "--target",
                commit,
                "--title",
                "Combined metadata catalog",
                "--notes",
                "Dataset metadata: catalog.jsonl.gz. Reference codes: "
                "codelists.jsonl.gz. Coverage and source failures: latest.json. "
                "Collection hashes and recovery index: current.json. "
                "Built offline from committed accepted adapter files.",
            ],
            check=True,
        )
        release = {"draft": True, "assets": []}
    if release["draft"]:
        uploaded = {asset["name"]: asset.get("digest") for asset in release["assets"]}
        pending = [
            str(path)
            for path in files
            if uploaded.get(path.name) != expected[path.name]
        ]
        if pending:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "upload",
                    tag,
                    *pending,
                    "--clobber",
                ],
                check=True,
            )
        release = _release(tag)
    if (
        release is None
        or {asset["name"]: asset.get("digest") for asset in release["assets"]}
        != expected
    ):
        raise ValueError("Release assets do not match the verified catalog candidate")
    verify_candidate(data, output)
    if release["draft"]:
        subprocess.run(
            [
                "gh",
                "release",
                "edit",
                tag,
                "--draft=false",
                "--latest",
            ],
            check=True,
        )
    return tag


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
    parser.add_argument("command", choices=["restore", "build", "publish"])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("output/catalog"))
    args = parser.parse_args()
    verify_published(args.data, [k for k, v in ADAPTERS.items() if v.available])
    if args.command == "restore":
        restore_assets(args.data)
        return
    if args.command == "build":
        result = build(args.data, args.output)
    else:
        publish_release(args.data, args.output)
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

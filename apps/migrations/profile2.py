"""Convert accepted collections and the existing publication once, entirely offline.

This relationship-only conversion preserves statistical payloads, ordering and
portal enrichment. It preserves codelist content and validated reference ranges,
reusing Zstandard bytes or transcoding legacy 7z without statistical reassembly.
"""

import argparse
import gzip
import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import zstandard

from apps.catalog.dependencies import dependencies
from apps.catalog.quality import validate_publication
from apps.migrations.profile_reader import normalize_agent_homepages, upgrade_document
from apps.model.models import Dataset
from apps.publication.archives import ASSETS, _compress, open_catalog, previous_asset
from apps.publication.artifacts import check_collection_sizes, manifest


def encoded(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def converted(document):
    upgraded, ledger = upgrade_document(document)
    normalize_agent_homepages(document)
    # Prove the payload and operational settings survive before model validation.
    for old, new in zip(
        document["dcat:distribution"], upgraded["dcat:distribution"], strict=True
    ):
        for key, value in old.items():
            if (
                key not in {"dcat:accessService", "ni:retrieval"}
                and new.get(key) != value
            ):
                raise ValueError(
                    f"Distribution information changed: {old['@id']}/{key}"
                )
        if old.get("ni:retrieval"):
            for key, value in old["ni:retrieval"].items():
                if key != "source" and new["ni:retrieval"].get(key) != value:
                    raise ValueError(f"Retrieval settings changed: {old['@id']}/{key}")
    for key, value in document.items():
        if (
            key
            not in {
                "@context",
                "ni:profileVersion",
                "catalogs",
                "dcat:distribution",
                "ni:sourceEvidence",
            }
            and upgraded.get(key) != value
        ):
            raise ValueError(f"Dataset information changed: {document['@id']}/{key}")
    dataset = Dataset.from_jsonld(upgraded)
    findings = validate_publication(dataset)
    return dataset, ledger, findings


def convert(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or source.is_relative_to(output)
    ):
        raise ValueError("A new, separate candidate directory is required")
    output.mkdir(parents=True)
    audit = output / "audit"
    audit.mkdir()
    target = output / "data"
    baseline = {
        str(p.relative_to(source)): digest(p)
        for p in source.glob("*/metadata/*.jsonl.gz")
    }
    summary = {
        "profile": "2.0.0",
        "accepted_datasets": 0,
        "contributions": 0,
        "published_datasets": 0,
        "attribute_preservation": "statistical and operational payloads exact",
        "collections": {},
        "reuse_check": "not run, per instruction",
        "reharvest": False,
    }
    # Only metadata is rewritten. Discovery state, checkpoints and native codelists
    # retain their original bytes; existing accepted filenames retain task recovery.
    for adapter in sorted(
        {p.parent.parent.name for p in source.glob("*/metadata/*.jsonl.gz")}
    ):
        root = source / adapter
        shutil.copytree(
            root, target / adapter, ignore=shutil.ignore_patterns("metadata")
        )
        destination = target / adapter / "metadata"
        destination.mkdir()
        with (audit / f"{adapter}.jsonl.gz").open("wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0
            ) as ledger_out:
                for path in sorted((root / "metadata").glob("*.jsonl.gz")):
                    count = 0
                    with (
                        gzip.open(path, "rt", encoding="utf-8") as incoming,
                        (destination / path.name).open("wb") as raw_out,
                    ):
                        with gzip.GzipFile(
                            filename="", fileobj=raw_out, mode="wb", mtime=0
                        ) as outgoing:
                            for line in incoming:
                                if not line.endswith("\n"):
                                    raise ValueError(f"Truncated input: {path}")
                                dataset, ledger, findings = converted(json.loads(line))
                                outgoing.write(encoded(dataset.to_jsonld()))
                                ledger["file"] = path.relative_to(source).as_posix()
                                ledger["line"] = count + 1
                                ledger["preservation"] = (
                                    "exact statistical and operational payloads"
                                )
                                ledger_out.write(encoded(ledger))
                                count += 1
                                summary["contributions"] += sum(
                                    d.retrieval is not None
                                    for d in dataset.distributions
                                )
                                if count % 500 == 0:
                                    print(adapter, path.name, count, flush=True)
                    summary["accepted_datasets"] += count
                    summary["collections"][path.relative_to(source).as_posix()] = {
                        "records": count,
                        "input_sha256": baseline[str(path.relative_to(source))],
                        "output_sha256": digest(destination / path.name),
                    }
        check_collection_sizes(target / adapter)
        print("accepted", adapter, summary["accepted_datasets"], flush=True)

    old_state = json.loads(
        (source / "catalog/state/current.json").read_text(encoding="utf-8")
    )
    for name, sha in old_state["assets"].items():
        if digest(source / "catalog" / name) != sha:
            raise ValueError(f"Published archive changed: {name}")
    published = target / "catalog"
    published.mkdir()
    new_state = deepcopy(old_state)
    new_state["profile_version"] = "2.0.0"
    groups = sorted(
        old_state["groups"].items(), key=lambda item: item[1]["catalog"]["offset"]
    )
    old_offset = 0
    content = output / "catalog.jsonl"
    with (
        open_catalog(
            source / "catalog" / previous_asset(old_state, "catalog")
        ) as incoming,
        content.open("wb") as outgoing,
        (audit / "publication.jsonl.gz").open("wb") as raw,
    ):
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as ledger_out:
            for group, state in groups:
                member = state["catalog"]
                if old_offset != member["offset"]:
                    raise ValueError("Invalid prior group offset")
                expected_end = old_offset + member["size"]
                before = hashlib.sha256()
                after = hashlib.sha256()
                start = outgoing.tell()
                findings_count = 0
                while old_offset < expected_end:
                    line = incoming.readline()
                    if not line.endswith(b"\n"):
                        raise ValueError("Truncated catalog member")
                    old_offset += len(line)
                    before.update(line)
                    dataset, ledger, findings = converted(json.loads(line))
                    used = {s.id for d in dataset.distributions for s in d.services}
                    for catalog in dataset.catalogs:
                        catalog.services = [s for s in catalog.services if s.id in used]
                    value = encoded(dataset.to_jsonld())
                    after.update(value)
                    outgoing.write(value)
                    findings_count += len(findings)
                    ledger_out.write(encoded(ledger))
                    summary["published_datasets"] += 1
                    if summary["published_datasets"] % 1000 == 0:
                        print("publication", summary["published_datasets"], flush=True)
                if old_offset != expected_end or before.hexdigest() != member["sha256"]:
                    raise ValueError(f"Prior catalog range integrity failed: {group}")
                new_state["groups"][group]["catalog"] = {
                    "offset": start,
                    "size": outgoing.tell() - start,
                    "sha256": after.hexdigest(),
                }
                new_state["groups"][group]["counts"]["quality_findings"] = (
                    findings_count
                )
        if incoming.read(1):
            raise ValueError("Unexpected extra catalog content")
    print("Compressing converted catalog once", flush=True)
    _compress(content, published / ASSETS[0])
    old_codes = source / "catalog" / previous_asset(old_state, "codelists")
    if old_codes.suffix == ".zst":
        shutil.copyfile(old_codes, published / ASSETS[1])
    else:
        with (
            open_catalog(old_codes) as incoming,
            (published / ASSETS[1]).open("wb") as raw,
        ):
            with zstandard.ZstdCompressor(level=6, write_checksum=True).stream_writer(
                raw
            ) as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    new_state["content_hashes"]["catalog"] = digest(content)
    new_state["assets"] = {name: digest(published / name) for name in ASSETS}
    for name, state in new_state["accepted"].items():
        candidate = target / name
        if candidate.exists():
            state["sha256"] = digest(candidate)
            state["blob"] = None  # Pinned to staged blobs at atomic cutover.
    for state in new_state["groups"].values():
        state["inputs"] = {
            name: new_state["accepted"][name]["sha256"]
            if name in new_state["accepted"]
            else value
            for name, value in state["inputs"].items()
        }
    new_state["dependencies"] = dependencies(source)
    new_state["rules"] = {
        k: v
        for k, v in new_state["dependencies"].items()
        if not k.startswith("portal:")
    }
    report = json.loads(
        (source / "catalog/reports/latest.json").read_text(encoding="utf-8")
    )
    report["counts"]["quality_findings"] = sum(
        g["counts"]["quality_findings"] for g in new_state["groups"].values()
    )
    report["profile_migration"] = {
        "from": "1.0.0",
        "to": "2.0.0",
        "codelists": "unchanged contents; legacy compression converted if necessary",
        "reharvest": False,
    }
    for filename, value in [
        ("state/current.json", new_state),
        ("reports/latest.json", report),
    ]:
        path = published / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    check_collection_sizes(published)
    if any(digest(source / name) != sha for name, sha in baseline.items()):
        raise ValueError("Accepted source changed during conversion")
    summary["publication"] = report["counts"]
    summary["assets"] = {
        name: {"sha256": sha, "bytes": (published / name).stat().st_size}
        for name, sha in new_state["assets"].items()
    }
    summary["candidate_manifest"] = manifest(target)
    (audit / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    content.unlink()
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in {"collections", "candidate_manifest"}
            }
        ),
        flush=True,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    convert(args.source, args.output)

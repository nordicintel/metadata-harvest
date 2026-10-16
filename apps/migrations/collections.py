"""Convert the committed legacy corpus offline into a separate, audited candidate."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.harvest.artifacts import checkpoint_path
from apps.harvest.metadata_adapters.pxweb_v1.mapping import map_metadata, source_record
from apps.harvest.registry import ADAPTERS, get_adapter
from apps.harvest.storage import JsonlStore
from apps.model.models import Dataset
from apps.model.vocabulary import PROFILE_VERSION
from apps.parsing.accepted import read_records, write_metadata
from apps.parsing.contributions import convert_dataset, legacy_document
from apps.publication.artifacts import check_collection_sizes, manifest
from retrieval._semantics import category_codes


def encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def convert(source: Path, output: Path, *, adapters=None):
    source, output = source.resolve(), output.resolve()
    if (
        source == output
        or output.is_relative_to(source)
        or source.is_relative_to(output)
    ):
        raise ValueError("Migration requires a separate candidate directory")
    output.mkdir(parents=True, exist_ok=True)
    audits = output.parent / "audit"
    audits.mkdir(exist_ok=True)
    summary_path = audits / "summary.json"
    summaries = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.exists()
        else {}
    )
    for adapter in adapters or [
        name for name, spec in ADAPTERS.items() if spec.available
    ]:
        root = source / adapter
        paths = sorted((root / "metadata").glob("*.jsonl*"))
        if not paths:
            raise ValueError(f"Missing source metadata for {adapter}")
        target = output / adapter
        target.mkdir(parents=True, exist_ok=True)
        report = {
            "source_records": 0,
            "verified_attributes": 0,
            "profile_version": PROFILE_VERSION,
            "input_hashes": manifest(root),
            "round_trip": "exact",
        }
        ledger_path = audits / (adapter + "-ledger.jsonl.gz")
        with (
            ledger_path.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as ledger,
        ):

            def contributions(paths=paths, adapter=adapter, report=report):
                for path in paths:
                    for line, document in enumerate(read_records(path), 1):
                        contribution = convert_dataset(document, adapter=adapter)
                        model = Dataset.from_jsonld(contribution.dataset.to_jsonld())
                        restored = legacy_document(model.distributions[0], model)
                        if restored != document:
                            raise ValueError(
                                f"Attribute preservation failed: {path}:{line}"
                            )
                        record = {
                            "file": path.relative_to(source).as_posix(),
                            "line": line,
                            "source": contribution.source.model_dump(),
                            "dataset": model.id,
                            "distribution": model.distributions[0].id,
                            "source_sha256": hashlib.sha256(
                                encoded(document).encode()
                            ).hexdigest(),
                            "attributes": contribution.attribute_mapping,
                            "round_trip": "exact",
                        }
                        ledger.write((encoded(record) + "\n").encode())
                        report["source_records"] += 1
                        report["verified_attributes"] += len(
                            contribution.attribute_mapping
                        )
                        yield contribution

            write_metadata(target, contributions(), adapter=adapter)
        # Copy operational records/state verbatim, independently of metadata conversion.
        for path in root.rglob("*"):
            if path.is_file() and path.relative_to(root).parts[0] != "metadata":
                destination = target / path.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        check_collection_sizes(target)
        report["output_hashes"] = manifest(target)
        (audits / (adapter + ".json")).write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        summaries[adapter] = {
            key: report[key]
            for key in ("source_records", "verified_attributes", "round_trip")
        }
        print(adapter, summaries[adapter], flush=True)
    if (source / "data_eu").exists():
        shutil.copytree(source / "data_eu", output / "data_eu", dirs_exist_ok=True)
    (audits / "summary.json").write_text(
        json.dumps(summaries, indent=2) + "\n", encoding="utf-8"
    )
    return summaries


def convert_collections(
    source: str | Path, candidate: str | Path, *, adapter: str
) -> dict:
    """Create a complete validated gzip candidate, leaving source bytes intact.

    Candidate must not exist. On failure no candidate is exposed. Completed
    conversions can be repeated into another directory and are byte-stable.
    Unfinished harvesting must complete before converting its output contract.
    """
    get_adapter(adapter)
    source, candidate = Path(source).resolve(), Path(candidate).resolve()
    if candidate.exists():
        raise FileExistsError(candidate)
    if (
        source == candidate
        or source in candidate.parents
        or candidate in source.parents
    ):
        raise ValueError("Source and candidate must be separate directories")
    if checkpoint_path(source).exists():
        raise ValueError("Complete unfinished harvesting before contract conversion")
    before = manifest(source)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix="metadata-conversion-", dir=candidate.parent
    ) as temporary:
        staged = Path(temporary) / "candidate"
        shutil.copytree(source, staged)
        store = JsonlStore(staged / "metadata", compressed=True)
        count = len(store.records)
        for key, native in store.records.items():
            record = source_record(native)
            mapped = map_metadata(native, record, adapter)
            if mapped["id"] != native["id"] or mapped["size"] != native["size"]:
                raise ValueError(f"Conversion changed dimensions: {key}")
            for code in native["id"]:
                if category_codes(
                    mapped["dimension"][code]["category"]
                ) != category_codes(native["dimension"][code]["category"]):
                    raise ValueError(f"Conversion changed category order: {key}/{code}")
            if source_record(mapped) != record:
                raise ValueError(f"Conversion changed source catalogue: {key}")
            store.records[key] = mapped
        store.save()
        summary = {
            "adapter": adapter,
            "records": count,
            "source_manifest": before,
            "contract": "metadata-harvest",
            "status": "validated_conversion",
        }
        check_collection_sizes(staged)
        if before != manifest(source):
            raise ValueError("Source changed during conversion")
        staged.rename(candidate)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adapter", action="append", dest="adapters")
    args = parser.parse_args()
    convert(args.source, args.output, adapters=args.adapters)

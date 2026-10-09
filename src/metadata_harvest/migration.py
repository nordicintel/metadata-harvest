"""Explicit whole-collection conversion; never mutate published inputs."""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from metadata_harvest.artifacts import check_collection_sizes, checkpoint_path, manifest
from metadata_harvest.mapping import map_metadata, source_record
from metadata_harvest.registry import get_adapter
from metadata_harvest.semantics import category_codes
from metadata_harvest.storage import JsonlStore, write


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
        write(staged / "reports/conversion.json", summary)
        check_collection_sizes(staged)
        if before != manifest(source):
            raise ValueError("Source changed during conversion")
        staged.rename(candidate)
    return summary

"""Build and apply accepted collections from an adapter's source files.

Parsing is manual and unscheduled: ``build`` reads ``data/<adapter>/source`` and
``state``, asks the adapter's ``parse`` module for worker documents, stamps the adapter name
into ``extension.nordicintel``, validates them and writes them as accepted
collections. ``apply``
replaces the committed collections only when they are unchanged since the build.
Each adapter's ``parse`` module provides ``documents(store, state, bindings, *,
portal=None)`` yielding ``(key, document | Exception)`` pairs and
``withheld_keys(state)``.
"""

import gzip
import importlib
import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

from apps.harvest.artifacts import report_status
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import read, write
from apps.parsing.accepted import write_metadata
from apps.publication.artifacts import check_collection_sizes, manifest
from registry import ServiceBinding
from schema.validation import validate_metadata

logger = logging.getLogger("parsing_logger")

REPORT = "reports/parse.json"


def load_state(root: Path) -> dict:
    """The adapter's operational state record, or an empty one."""
    path = Path(root) / "state/current.jsonl.gz"
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        line = stream.readline()
    return json.loads(line) if line.strip() else {}


def _metadata_manifest(root: Path) -> dict:
    directory = Path(root) / "metadata"
    return manifest(directory) if directory.exists() else {}


def committed_datasets(root: Path) -> int:
    """Datasets in the committed accepted collections of ``data/<adapter>``."""
    directory = Path(root) / "metadata"
    count = 0
    for path in sorted(directory.glob("*.jsonl.gz")) if directory.exists() else []:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            count += sum(1 for line in stream if line.strip())
    return count


def build(
    adapter: str,
    data: Path,
    output: Path,
    *,
    bindings: list[ServiceBinding],
    portal: Path | None = None,
) -> dict:
    """Parse one adapter's source files into ``output/metadata`` and report."""
    data, output = Path(data), Path(output)
    root = data / adapter
    if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
        raise ValueError("Parse output must be separate from published data")
    module = importlib.import_module(f"apps.harvest.metadata_adapters.{adapter}.parse")
    store = SourceStore(root / "source", adapter)
    state = load_state(root)
    withheld = set(module.withheld_keys(state))
    documents, errors = [], []
    counts = {"parsed": 0, "failed": 0, "withheld": 0}
    for key, item in module.documents(store, state, bindings, portal=portal):
        if key in withheld:
            counts["withheld"] += 1
            continue
        if not isinstance(item, Exception):
            item["extension"]["nordicintel"]["adapter"] = adapter
            try:
                validate_metadata(item, adapter=adapter)
            except Exception as error:  # the document, not the run, is at fault
                item = error
        if isinstance(item, Exception):
            counts["failed"] += 1
            errors.append({"key": key, "error": f"{type(item).__name__}: {item}"})
            logger.error(
                "%s: %s failed to parse: %s", adapter, key, errors[-1]["error"]
            )
            continue
        counts["parsed"] += 1
        documents.append(item)
    if output.exists():
        if any(output.iterdir()) and not (output / "result.json").exists():
            raise ValueError(f"{output} is not an earlier parse output; refusing")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    written = write_metadata(
        output, documents, empty_providers=[binding.source_code for binding in bindings]
    )
    result = {
        "adapter": adapter,
        "status": report_status(pending=0, errors=errors),
        "counts": counts,
        "source_entries": len(store.entries()),
        "committed_datasets": committed_datasets(root),
        "errors": errors,
        "collections": sorted(path.name for path in written),
    }
    if result["source_entries"] == 0:
        logger.warning(
            "%s has no source entries yet; nothing would be applied", adapter
        )
    elif counts["parsed"] < result["committed_datasets"]:
        logger.warning(
            "%s parsed %d datasets, fewer than the %d committed; apply needs "
            "--allow-shrink",
            adapter,
            counts["parsed"],
            result["committed_datasets"],
        )
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=_metadata_manifest(root), manifest=manifest(output))
    write(output / "result.json", result)
    logger.info("%s parse finished: %s %s", adapter, result["status"], counts)
    return result


def apply(
    adapter: str, data: Path, output: Path, *, allow_shrink: bool = False
) -> dict:
    """Replace ``data/<adapter>/metadata`` with the built collections.

    Refuses while the adapter has no source entries (the harvest has not filled
    ``source/`` yet) and, unless ``allow_shrink``, when fewer datasets parsed than
    the committed collections hold: during the cutover a partial source tree must
    not shrink accepted data.
    """
    data, output = Path(data), Path(output)
    root = data / adapter
    result = read(output / "result.json")
    if _metadata_manifest(root) != result["base_manifest"]:
        raise ValueError(f"{adapter} collections changed since the parse build")
    if result.get("source_entries", 0) == 0:
        raise ValueError(f"{adapter} has no source entries; nothing to apply")
    if result["counts"]["parsed"] < result.get("committed_datasets", 0) and (
        not allow_shrink
    ):
        raise ValueError(
            f"{adapter} parsed {result['counts']['parsed']} datasets, fewer than the "
            f"{result['committed_datasets']} committed; pass --allow-shrink to accept"
        )
    if manifest(output) != result["manifest"]:
        raise ValueError(f"{adapter} parse artifact checksum mismatch")
    check_collection_sizes(output)
    metadata = root / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    staged = {}
    try:
        for name in result["collections"]:
            temporary = metadata / f".{uuid4().hex}.tmp"
            shutil.copyfile(output / "metadata" / name, temporary)
            staged[metadata / name] = temporary
        for target, temporary in staged.items():
            temporary.replace(target)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    for path in metadata.glob("*.jsonl*"):
        if path.name not in result["collections"]:
            path.unlink()
    (root / "reports").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(output / REPORT, root / REPORT)
    return result

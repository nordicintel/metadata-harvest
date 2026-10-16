"""Build and apply accepted collections from an adapter's source files.

Parsing is manual and unscheduled: ``build`` reads ``data/<adapter>/source`` and
``state``, asks the adapter's ``parse`` module for worker documents, validates them
and writes the same accepted collections the harvest used to write. ``apply``
replaces the committed collections only when they are unchanged since the build.
Each adapter's ``parse`` module provides ``documents(store, state, providers, *,
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

from apps.configuration import providers_for_adapter
from apps.harvest.artifacts import report_status
from apps.harvest.source_store import SourceStore
from apps.harvest.storage import read, write
from apps.harvest.validation import validate_metadata
from apps.parsing.accepted import write_metadata
from apps.publication.artifacts import check_collection_sizes, manifest

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


def build(
    adapter: str,
    data: Path,
    output: Path,
    *,
    providers: list[dict] | None = None,
    portal: Path | None = None,
) -> dict:
    """Parse one adapter's source files into ``output/metadata`` and report."""
    data, output = Path(data), Path(output)
    root = data / adapter
    if root.resolve() == output.resolve() or root.resolve() in output.resolve().parents:
        raise ValueError("Parse output must be separate from published data")
    providers = providers if providers is not None else providers_for_adapter(adapter)
    module = importlib.import_module(f"apps.harvest.metadata_adapters.{adapter}.parse")
    store = SourceStore(root / "source", adapter)
    state = load_state(root)
    withheld = set(module.withheld_keys(state))
    documents, errors = [], []
    counts = {"parsed": 0, "failed": 0, "withheld": 0}
    for key, item in module.documents(store, state, providers, portal=portal):
        if key in withheld:
            counts["withheld"] += 1
            continue
        if not isinstance(item, Exception):
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
        shutil.rmtree(output)
    output.mkdir(parents=True)
    written = write_metadata(
        output,
        documents,
        adapter=adapter,
        empty_providers=[provider["code"] for provider in providers],
    )
    result = {
        "adapter": adapter,
        "status": report_status(pending=0, errors=errors),
        "counts": counts,
        "errors": errors,
        "collections": sorted(path.name for path in written),
    }
    write(output / REPORT, result)
    check_collection_sizes(output)
    result.update(base_manifest=_metadata_manifest(root), manifest=manifest(output))
    write(output / "result.json", result)
    logger.info("%s parse finished: %s %s", adapter, result["status"], counts)
    return result


def apply(adapter: str, data: Path, output: Path) -> dict:
    """Replace ``data/<adapter>/metadata`` with the built collections."""
    data, output = Path(data), Path(output)
    root = data / adapter
    result = read(output / "result.json")
    if _metadata_manifest(root) != result["base_manifest"]:
        raise ValueError(f"{adapter} collections changed since the parse build")
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

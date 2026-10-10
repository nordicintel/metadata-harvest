"""One-off import of the RowStore proof-of-concept results as the accepted baseline.

Reads ``metadata.jsonl`` (one structure document per harvested table) and
``state.json`` (markers, status and cost per table) from the proof of concept's
output directory, trims wide columns, writes ``state/tables.jsonl.gz`` and
publishes the derived collections offline. No request is made: those results
cost thousands of requests the hosts now refuse.

    uv run --extra harvest python scripts/import_entrysoft.py <rowstore_final> --data data/entrysoft
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from apps.configuration import providers_for_adapter
from apps.harvest.metadata_adapters.entrysoft.datasets import trim
from apps.harvest.metadata_adapters.entrysoft.state import load_tables, table_key
from apps.harvest.metadata_adapters.entrysoft.workflow import markers, publish
from apps.harvest.storage import write


def import_results(source: Path, data: Path, providers: list[dict]) -> dict:
    by_code = {p["code"]: p for p in providers}
    state = json.loads((source / "state.json").read_text(encoding="utf-8"))
    tables = {}
    with (source / "metadata.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            document = json.loads(line)
            record = document["extension"]["harvesting"]
            key = table_key(record)
            previous = state.get(key, {})
            tables[key] = {
                "key": key,
                "provider_code": record["provider_code"],
                "dataset_code": record["dataset_code"],
                "record": record,
                "markers": previous.get("markers") or markers(record),
                "harvested_at": previous.get("harvested_at")
                or datetime.now(UTC).isoformat(),
                "status": "ok",
                "rowcount": previous.get(
                    "rowcount", document["extension"]["rowstore"]["rowcount"]
                ),
                "strategy": previous.get("strategy"),
                "requests": previous.get("requests"),
                "document": trim(document),
            }
    for key, previous in state.items():
        if key in tables or previous.get("status") not in ("empty", "deferred"):
            continue
        provider_code, dataset_code = key.split("/", 1)
        provider = by_code.get(provider_code)
        if provider is None:
            continue
        tables[key] = {
            "key": key,
            "provider_code": provider_code,
            "dataset_code": dataset_code,
            "record": {
                "provider_code": provider_code,
                "dataset_code": dataset_code,
                "language": "sv",
                "base_url": provider["base_url"],
                "table_url": f"{provider['base_url']}/rowstore/dataset/{dataset_code}",
                "title": None,
                "catalogue": {},
            },
            "markers": previous.get("markers") or {},
            "harvested_at": previous.get("harvested_at"),
            "status": previous["status"],
            "rowcount": previous.get("rowcount"),
            "reason": previous.get("reason"),
            "paging_requests": previous.get("paging_requests"),
            "document": None,
        }
    current = {
        "mapping_version": None,
        "providers": {
            code: {"checked_at": None, "complete": None, "imported": True}
            for code in sorted({t["provider_code"] for t in tables.values()})
        },
        "dropped": [],
    }
    summary = publish(tables, current, providers, data)
    counts = {
        "tables": len(tables),
        "ok": sum(t["status"] == "ok" for t in tables.values()),
        "empty": sum(t["status"] == "empty" for t in tables.values()),
        "deferred": sum(t["status"] == "deferred" for t in tables.values()),
        **summary,
    }
    write(
        data / "reports/latest.json",
        {
            "run_id": "import",
            "status": "success",
            "continue": False,
            "interrupted": None,
            "selection": None,
            "counts": {
                "unchanged": 0,
                "harvested": 0,
                "empty": counts["empty"],
                "deferred": counts["deferred"],
                "removed": 0,
                "failed": 0,
                "datasets": counts["datasets"],
                "lists": len(counts["lists"]),
            },
            "requests": 0,
            "imported_from": str(source),
            "deferred_tables": [
                {"table": k, "rowcount": t.get("rowcount"), "reason": t.get("reason")}
                for k, t in sorted(tables.items())
                if t["status"] == "deferred"
            ],
            "dropped_tables": summary["dropped"],
            "list_datasets": summary["lists"],
            "errors": [],
        },
    )
    return counts


def compare(data: Path, poc_datasets: Path) -> list[str]:
    """Differences between the published grouping and the proof of concept's."""
    expected = {}
    with poc_datasets.open(encoding="utf-8") as stream:
        for line in stream:
            dataset = json.loads(line)
            if dataset["kind"] == "cube":
                expected[tuple(sorted(dataset["tables"]))] = dataset
    from apps.harvest.metadata_adapters.entrysoft.state import collection_store

    published = {
        tuple(sorted(d["extension"]["entrysoft"]["tables"])): d
        for d in collection_store(data).records.values()
    }
    problems = []
    for tables in sorted(set(expected) ^ set(published)):
        where = "proof of concept" if tables in expected else "import"
        problems.append(f"{tables[0]}: only in {where}")
    for tables in sorted(set(expected) & set(published)):
        listed = [d["id"] for d in expected[tables]["dimensions"] if "categories" in d]
        if listed + ["measure"] != published[tables]["id"]:
            problems.append(
                f"{tables[0]}: dimensions {published[tables]['id']} != {listed}"
            )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--data", type=Path, default=Path("data/entrysoft"))
    parser.add_argument("--providers", type=Path, default=None)
    args = parser.parse_args()
    if load_tables(args.data):
        raise SystemExit("Refusing to import over existing EntryScape state")
    counts = import_results(
        args.source,
        args.data,
        providers_for_adapter("entrysoft", legacy_path=args.providers),
    )
    print(
        json.dumps(
            {k: v for k, v in counts.items() if k != "dropped"}, ensure_ascii=False
        )
    )
    problems = compare(args.data, args.source / "datasets.jsonl")
    print("\n".join(problems) if problems else "grouping matches the proof of concept")
    raise SystemExit(bool(problems))

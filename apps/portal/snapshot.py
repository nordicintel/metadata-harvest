"""Import and refresh complete scoped portal snapshots before acceptance."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

SEARCH_URL = "https://data.europa.eu/api/hub/search"


def records(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith("\n"):
                raise ValueError(f"Truncated JSONL line {number}")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Invalid record at line {number}")
            yield value


def import_snapshot(
    source: Path, target: Path, *, expected_count: int | None = None
) -> dict:
    """Accept a complete snapshot; invalid imports never replace accepted bytes."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="portal-", dir=target.parent) as temporary:
        candidate = Path(temporary) / "oppnadata.jsonl.gz"
        seen = set()
        with (
            candidate.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out,
        ):
            for record in records(Path(source)):
                identifier = record.get("id")
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier in seen
                ):
                    raise ValueError("Missing or duplicate portal identity")
                if record.get("catalog", {}).get("id") != "oppnadata":
                    raise ValueError("Portal record belongs to another catalog")
                seen.add(identifier)
                out.write(
                    (
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                        + "\n"
                    ).encode()
                )
        if not seen or expected_count is not None and len(seen) != expected_count:
            raise ValueError("Incomplete portal snapshot")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        report = {
            "count": len(seen),
            "sha256": digest,
            "catalog": "oppnadata",
            "query": {"filters": "dataset", "facets": {"catalog": ["oppnadata"]}},
            "coverage": {
                "complete": True,
                "expected_count": expected_count,
                "identifiers_sha256": hashlib.sha256(
                    "\n".join(sorted(seen)).encode()
                ).hexdigest(),
            },
            "source": SEARCH_URL,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "status": "success",
        }
        target.mkdir(parents=True, exist_ok=True)
        current = target / candidate.name
        if not current.exists() or current.read_bytes() != candidate.read_bytes():
            candidate.replace(current)
            state = target / "state.json"
            stage = target / ".state.json.tmp"
            stage.write_text(
                json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            stage.replace(state)
        elif (target / "state.json").exists():
            old = json.loads((target / "state.json").read_text(encoding="utf-8"))
            report["retrieved_at"] = old.get("retrieved_at", report["retrieved_at"])
            if report != old:
                stage = target / ".state.json.tmp"
                stage.write_text(
                    json.dumps(report, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                stage.replace(target / "state.json")
        return report


def accepted_records(target: Path):
    """Verify the accepted snapshot's manifest, scope and full record count offline."""
    path = target / "oppnadata.jsonl.gz"
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != state["sha256"]:
            raise ValueError("Portal snapshot checksum mismatch")
    count = 0
    for record in records(path):
        if record.get("catalog", {}).get("id") != "oppnadata":
            raise ValueError("Invalid accepted portal scope")
        count += 1
        yield record
    if count != state["count"]:
        raise ValueError("Incomplete accepted portal coverage")


def refresh_snapshot(target: Path, *, client=None) -> dict:
    """Fetch a complete search scroll before exposing it to offline builders."""
    own = client is None
    client = client or httpx.Client(timeout=120, follow_redirects=True)
    try:
        response = client.get(
            SEARCH_URL + "/search",
            params={
                "filters": "dataset",
                "limit": 200,
                "scroll": "true",
                "facets": json.dumps({"catalog": ["oppnadata"]}),
            },
        )
        response.raise_for_status()
        result = response.json()["result"]
        expected = result["count"]
        scroll = result.get("scrollId")
        batch = result.get("results", [])
        with TemporaryDirectory(prefix="portal-download-") as temporary:
            path = Path(temporary) / "snapshot.jsonl"
            count = 0
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                while batch:
                    for record in batch:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += len(batch)
                    if count >= expected:
                        break
                    if not scroll:
                        raise ValueError("Missing portal scroll cursor")
                    response = client.get(
                        SEARCH_URL + "/scroll", params={"scrollId": scroll}
                    )
                    response.raise_for_status()
                    result = response.json()["result"]
                    scroll = result.get("scrollId", scroll)
                    batch = result.get("results", [])
            return import_snapshot(path, target, expected_count=expected)
    finally:
        if own:
            client.close()

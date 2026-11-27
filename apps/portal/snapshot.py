"""Import and refresh complete scoped portal snapshots before acceptance."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
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


# A raw-size target also bounds memory when records compress especially well.
PART_TARGET_BYTES = 64 * 1024 * 1024
LEGACY_FILE = "oppnadata.jsonl.gz"


def write_part(directory, data, digest):
    compressed = gzip.compress(data, compresslevel=6, mtime=0)
    if len(compressed) >= 100 * 1024 * 1024:
        raise ValueError("Portal record exceeds GitHub's file limit")
    digest.update(compressed)
    name = f"oppnadata-{hashlib.sha256(compressed).hexdigest()}.jsonl.gz"
    (directory / name).write_bytes(compressed)
    return name


def import_snapshot(
    source: Path, target: Path, *, expected_count: int | None = None
) -> dict:
    """Accept a complete snapshot; invalid imports never replace accepted bytes."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="portal-", dir=target.parent) as temporary:
        directory = Path(temporary)
        seen = set()
        parts = []
        digest = hashlib.sha256()
        chunk = bytearray()
        for record in records(Path(source)):
            identifier = record.get("id")
            if not isinstance(identifier, str) or not identifier or identifier in seen:
                raise ValueError("Missing or duplicate portal identity")
            if record.get("catalog", {}).get("id") != "oppnadata":
                raise ValueError("Portal record belongs to another catalog")
            seen.add(identifier)
            line = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            if chunk and len(chunk) + len(line) > PART_TARGET_BYTES:
                parts.append(write_part(directory, chunk, digest))
                chunk.clear()
            chunk.extend(line)
        if not seen or expected_count is not None and len(seen) != expected_count:
            raise ValueError("Incomplete portal snapshot")
        parts.append(write_part(directory, chunk, digest))
        report = {
            "count": len(seen),
            "sha256": digest.hexdigest(),
            "parts": parts,
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
        state = target / "state.json"
        old = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        if old.get("sha256") == report["sha256"]:
            report["retrieved_at"] = old["retrieved_at"]
        for name in parts:
            (directory / name).replace(target / name)
        stage = directory / "state.json"
        stage.write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        stage.replace(state)
        # Switch the manifest before retiring any files from the previous snapshot.
        for path in target.glob("oppnadata-*.jsonl.gz"):
            if path.name not in parts:
                path.unlink()
        (target / LEGACY_FILE).unlink(missing_ok=True)
        return report


def accepted_records(target: Path):
    """Verify the accepted snapshot's manifest, scope and full record count offline."""
    state = json.loads((target / "state.json").read_text(encoding="utf-8"))
    parts = state.get("parts", [LEGACY_FILE])
    digest = hashlib.sha256()
    for name in parts:
        if name != LEGACY_FILE and not re.fullmatch(
            r"oppnadata-[0-9a-f]{64}\.jsonl\.gz", name
        ):
            raise ValueError("Invalid portal part name")
        with (target / name).open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    if digest.hexdigest() != state["sha256"]:
        raise ValueError("Portal snapshot checksum mismatch")
    count = 0
    for name in parts:
        for record in records(target / name):
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
                "limit": 500,
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

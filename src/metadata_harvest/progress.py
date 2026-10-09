"""Small current worker status, independent of accepted metadata and checkpoints."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


def utc_now():
    return datetime.now(UTC).isoformat()


class Progress:
    """Write atomic counters and an append-only temporary error transport file.

    HARVEST_PROGRESS must point outside the adapter's certified output tree.
    Neither file is a publication input or a metadata history store.
    """

    def __init__(self, adapter: str, provider: str):
        target = os.environ.get("HARVEST_PROGRESS")
        self.path = Path(target) if target else None
        self.started = time.monotonic()
        self.queue_started = self.started
        self.state = {
            "adapter": adapter,
            "provider": provider,
            "phase": "starting",
            "started_at": utc_now(),
            "total": None,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "reused": 0,
            "codelists_succeeded": 0,
            "errors_by_stage": {},
            "error_count": 0,
        }
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.with_suffix(".errors.jsonl").write_text("", encoding="utf-8")
        self.update()

    def update(self, **values):
        if values.get("phase") == "metadata" and self.state["phase"] != "metadata":
            self.queue_started = time.monotonic()
        self.state.update(values)
        state = self.state
        state["updated_at"] = utc_now()
        state["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        state["remaining"] = (
            max(0, state["total"] - state["processed"])
            if state["total"] is not None
            else None
        )
        state["eta_seconds"] = (
            round(
                (time.monotonic() - self.queue_started)
                / state["processed"]
                * state["remaining"]
            )
            if state["phase"] == "metadata"
            and state["processed"] >= 5
            and state["remaining"] is not None
            else None
        )
        if self.path:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(self.path)

    def error(self, stage: str, resource: str, language: str, error: str, **details):
        self.state["error_count"] += 1
        counts = self.state["errors_by_stage"]
        counts[stage] = counts.get(stage, 0) + 1
        entry = {
            "number": self.state["error_count"],
            "at": utc_now(),
            "adapter": self.state["adapter"],
            "provider": self.state["provider"],
            "stage": stage,
            "resource": resource,
            "language": language,
            "error": str(error),
            **details,
        }
        if self.path:
            with self.path.with_suffix(".errors.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(json.dumps({"harvest_error": entry}, ensure_ascii=False), flush=True)
        self.update()

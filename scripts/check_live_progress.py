"""Prove Checks API visibility during a bounded synthetic worker, without harvesting."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from metadata_harvest.monitor import github_api, inspect_run
from metadata_harvest.progress import Progress


def worker():
    progress = Progress("test", "synthetic")
    progress.update(phase="metadata", total=2)
    progress.error(
        "codelist",
        "synthetic-code",
        "sv",
        "Intentional monitoring test error",
        url="https://example.invalid/codelists/synthetic-code",
    )
    progress.update(processed=1, succeeded=1, current="synthetic-table-2")
    time.sleep(45)
    progress.update(
        processed=2,
        succeeded=2,
        phase="finished",
        result_status="completed_with_errors",
    )


def check():
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "metadata_harvest.monitor",
            "run",
            "--name",
            "visibility-smoke",
            "--",
            sys.executable,
            __file__,
            "--worker",
        ]
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and process.poll() is None:
            results = inspect_run(
                os.environ["GITHUB_REPOSITORY"],
                os.environ["GITHUB_RUN_ID"],
                errors=True,
            )
            for result in results:
                state = result["progress"]
                if (
                    result["worker"] == "visibility-smoke"
                    and state.get("processed") == 1
                ):
                    assert result["check_status"] == "in_progress"
                    assert not result["stale"]
                    assert result["errors"][0]["resource"] == "synthetic-code"
                    assert (
                        result["errors"][0]["error"]
                        == "Intentional monitoring test error"
                    )
                    assert process.poll() is None
                    evidence = {"verified_while_worker_running": True, "result": result}
                    Path("output").mkdir(exist_ok=True)
                    Path("output/live-progress-verification.json").write_text(
                        json.dumps(evidence, indent=2), encoding="utf-8"
                    )
                    print(json.dumps(evidence), flush=True)
                    assert process.wait(timeout=65) == 0
                    github_api(
                        f"repos/{os.environ['GITHUB_REPOSITORY']}/check-runs/{result['check_id']}",
                        method="PATCH",
                        payload={
                            "conclusion": "neutral",
                            "output": {
                                "title": "Live visibility verified with an intentional synthetic error",
                                "summary": "Counts and full individual error were read back before the worker exited. No source API requests or production writes.",
                            },
                        },
                    )
                    return
            time.sleep(3)
        raise RuntimeError(
            "Could not inspect counts and individual error while worker was running"
        )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)


if __name__ == "__main__":
    worker() if "--worker" in sys.argv else check()

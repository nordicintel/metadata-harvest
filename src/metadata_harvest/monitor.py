"""Publish and inspect worker progress while Actions logs are still unavailable."""

import argparse
import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from metadata_harvest.progress import utc_now


def github_api(path, *, method="GET", payload=None, paginate=False):
    args = ["gh", "api", path, "--method", method]
    if payload is not None:
        args.extend(["--input", "-"])
    if paginate:
        args.extend(["--paginate", "--slurp"])
    try:
        result = subprocess.run(
            args,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise OSError(f"GitHub API {method} {path}: {error.stderr.strip()}") from error
    return json.loads(result.stdout)


def error_annotations(errors):
    annotations = []
    for error in errors:
        encoded = json.dumps(error, ensure_ascii=False)
        if len(encoded.encode("utf-8")) < 50_000:
            messages = [encoded]
        else:
            chunks = [
                encoded[offset : offset + 8000]
                for offset in range(0, len(encoded), 8000)
            ]
            messages = [
                json.dumps(
                    {
                        "number": error["number"],
                        "fragment_index": index,
                        "fragment_count": len(chunks),
                        "fragment": chunk,
                    },
                    ensure_ascii=False,
                )
                for index, chunk in enumerate(chunks)
            ]
        for message in messages:
            annotations.append(
                {
                    "path": "harvest-errors",
                    "start_line": max(1, error["number"]),
                    "end_line": max(1, error["number"]),
                    "annotation_level": "warning",
                    "title": f"{error['stage']}: {error['resource']} ({error['language']})"[
                        :250
                    ],
                    "message": message,
                }
            )
    return annotations


def decode_errors(annotations):
    complete, fragments = {}, {}
    for annotation in annotations:
        error = json.loads(annotation["message"])
        if "fragment_index" in error:
            fragments.setdefault(error["number"], {})[error["fragment_index"]] = error
        else:
            complete[error["number"]] = error
    for number, parts in fragments.items():
        count = next(iter(parts.values()))["fragment_count"]
        complete[number] = (
            json.loads("".join(parts[index]["fragment"] for index in range(count)))
            if set(parts) == set(range(count))
            else {
                "number": number,
                "incomplete_details": True,
                "parts_received": len(parts),
                "parts_expected": count,
            }
        )
    return [complete[number] for number in sorted(complete)]


class CheckReporter:
    """One mutable check per worker, with inspectable annotations for every error."""

    def __init__(self, name, *, api=github_api):
        self.api = api
        self.name = name
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.run_id = os.environ["GITHUB_RUN_ID"]
        self.external_id = (
            f"harvest:{self.run_id}:{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}:{name}"
        )
        self.sent = 0

    def start(self):
        result = self.api(
            f"repos/{self.repo}/check-runs",
            method="POST",
            payload={
                "name": f"Harvest progress: {self.name}",
                "head_sha": os.environ["GITHUB_SHA"],
                "external_id": self.external_id,
                "status": "in_progress",
                "started_at": utc_now(),
                "details_url": f"https://github.com/{self.repo}/actions/runs/{self.run_id}",
                "output": {
                    "title": "Verifying live monitoring",
                    "summary": "Worker has not started.",
                },
            },
        )
        self.path = f"repos/{self.repo}/check-runs/{result['id']}"
        pointer = Path(
            os.environ.get("HARVEST_CHECK_POINTER", "output/telemetry/check.json")
        )
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps({"path": self.path, "external_id": self.external_id}),
            encoding="utf-8",
        )
        # Never launch a long worker until its telemetry can actually be read back.
        observed = self.api(self.path)
        if observed.get("external_id") != self.external_id:
            raise RuntimeError("Progress check identity could not be verified")
        print(f"Live progress: {self.path}", flush=True)

    def update(self, state, errors, *, conclusion=None):
        state = {**state, "reported_at": utc_now()}
        if state.get("updated_at"):
            state["worker_update_age_seconds"] = round(
                (
                    datetime.now(UTC) - datetime.fromisoformat(state["updated_at"])
                ).total_seconds(),
                1,
            )
        title = (
            f"{state.get('phase', 'starting')}: "
            f"{state.get('processed', 0)}/{state.get('total', '?')} processed; "
            f"{state.get('succeeded', 0)} accepted; {state.get('failed', 0)} table failures; "
            f"{state.get('errors_by_stage', {}).get('codelist', 0)} codelist errors"
        )
        summary = (
            title
            + "\n\nReported: "
            + state["reported_at"]
            + "\n\nCounters and current resource are in the JSON below. "
            "Each error is independently available through this check's annotations API. "
            "ETA is a measured table-queue estimate, excludes final publication, and is unknown until five tasks finish."
        )
        annotations = error_annotations(errors)
        while True:
            batch = annotations[self.sent : self.sent + 50]
            output = {
                "title": title[:250],
                "summary": summary,
                "text": json.dumps(state, ensure_ascii=False),
            }
            if batch:
                output["annotations"] = batch
            payload = {"output": output}
            last = self.sent + len(batch) >= len(annotations)
            if conclusion and last:
                payload.update(
                    status="completed", conclusion=conclusion, completed_at=utc_now()
                )
            self.api(self.path, method="PATCH", payload=payload)
            self.sent += len(batch)
            if last:
                break


def read_progress(path):
    state = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"phase": "starting"}
    )
    error_path = path.with_suffix(".errors.jsonl")
    errors = []
    if error_path.exists():
        # The final line may still be in flight. Never acknowledge an incomplete line.
        for line in error_path.read_text(encoding="utf-8").splitlines(keepends=True):
            if line.endswith("\n"):
                errors.append(json.loads(line))
    return state, errors


def stop_worker(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_monitored(name, command, *, interval=30):
    reporter = CheckReporter(name)
    reporter.start()
    path = Path(os.environ.get("HARVEST_PROGRESS", "output/telemetry/progress.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.with_suffix(".errors.jsonl").unlink(missing_ok=True)
    environment = {**os.environ, "HARVEST_PROGRESS": str(path), "PYTHONUNBUFFERED": "1"}
    interrupted = False

    def cancel(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous_handlers = {
        sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    process = None
    failures = 0
    next_report = 0
    try:
        process = subprocess.Popen(command, env=environment)
        while process.poll() is None and not interrupted:
            if time.monotonic() >= next_report:
                state, errors = read_progress(path)
                try:
                    reporter.update(state, errors)
                    failures = 0
                except (subprocess.SubprocessError, OSError, ValueError) as error:
                    failures += 1
                    print(
                        f"Live monitoring update failed ({failures}/3): {error}",
                        flush=True,
                    )
                    if failures >= 3:
                        raise RuntimeError(
                            "Live monitoring unavailable; stopping worker and retaining existing snapshots"
                        ) from error
                next_report = time.monotonic() + interval
            time.sleep(0.5)
        if interrupted:
            stop_worker(process)
        else:
            process.wait()
        state, errors = read_progress(path)
        unhealthy = errors or state.get("result_status") in {
            "running",
            "interrupted",
            "completed_with_errors",
        }
        conclusion = (
            "cancelled"
            if interrupted
            else "failure"
            if process.returncode or unhealthy
            else "success"
        )
        state.update(
            worker_exit_code=process.returncode,
            worker_state="cancelled" if interrupted else "exited",
        )
        reporter.update(state, errors, conclusion=conclusion)
        # A table error is reported by the collector, not an excuse to discard its artifact.
        return 130 if interrupted else process.returncode
    except Exception as error:
        if process is not None:
            stop_worker(process)
        state, errors = read_progress(path)
        state.update(
            worker_state="stopped", monitoring_error=f"{type(error).__name__}: {error}"
        )
        try:
            reporter.update(state, errors, conclusion="failure")
        except (subprocess.SubprocessError, OSError, ValueError):
            pass  # The existing check becomes stale; never claim successful monitoring.
        raise
    finally:
        if process is not None:
            stop_worker(process)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def inspect_run(repo, run_id, *, errors=False, api=github_api):
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    prefix = f"harvest:{run_id}:{run['run_attempt']}:"
    pages = api(
        f"repos/{repo}/commits/{run['head_sha']}/check-runs?filter=all&per_page=100",
        paginate=True,
    )
    result = []
    for page in pages:
        for check in page["check_runs"]:
            if not (check.get("external_id") or "").startswith(prefix):
                continue
            raw = check["output"].get("text")
            state = json.loads(raw) if raw else {"phase": "starting"}
            reported = state.get("reported_at")
            age = (
                (datetime.now(UTC) - datetime.fromisoformat(reported)).total_seconds()
                if reported
                else None
            )
            item = {
                "worker": check["external_id"][len(prefix) :],
                "check_id": check["id"],
                "check_status": check["status"],
                "workflow_status": run["status"],
                "stale": check["status"] != "completed"
                and (age is None or age > 90 or run["status"] == "completed"),
                "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                "progress": state,
            }
            if errors:
                annotations = api(
                    f"repos/{repo}/check-runs/{check['id']}/annotations?per_page=100",
                    paginate=True,
                )
                item["errors"] = decode_errors(
                    [annotation for batch in annotations for annotation in batch]
                )
            result.append(item)
    return result


def close_check(conclusion, *, api=github_api):
    """Always-step fallback if a runner signal killed the supervising process."""
    pointer = Path(
        os.environ.get("HARVEST_CHECK_POINTER", "output/telemetry/check.json")
    )
    if not pointer.exists():
        return
    reference = json.loads(pointer.read_text(encoding="utf-8"))
    prefix = f"harvest:{os.environ['GITHUB_RUN_ID']}:{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}:"
    repo_prefix = f"repos/{os.environ['GITHUB_REPOSITORY']}/check-runs/"
    if not reference["external_id"].startswith(prefix) or not reference[
        "path"
    ].startswith(repo_prefix):
        raise ValueError("Refusing to close another run's progress check")
    check = api(reference["path"])
    if check.get("external_id") != reference["external_id"]:
        raise ValueError("Progress check identity changed")
    if check["status"] != "completed":
        api(
            reference["path"],
            method="PATCH",
            payload={
                "status": "completed",
                "conclusion": conclusion,
                "completed_at": utc_now(),
            },
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    close = sub.add_parser("close")
    close.add_argument(
        "--conclusion", choices=["failure", "cancelled"], default="failure"
    )
    worker = sub.add_parser("run")
    worker.add_argument("--name", required=True)
    worker.add_argument("worker_command", nargs=argparse.REMAINDER)
    status = sub.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--repo", default="nordicintel/metadata-harvest")
    status.add_argument("--errors", action="store_true")
    status.add_argument("--worker")
    status.add_argument("--error", type=int)
    args = parser.parse_args()
    if args.command == "close":
        close_check(args.conclusion)
        return
    if args.command == "run":
        command = args.worker_command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("A worker command is required")
        raise SystemExit(run_monitored(args.name, command))
    results = inspect_run(
        args.repo, args.run_id, errors=args.errors or args.error is not None
    )
    if args.worker:
        results = [r for r in results if r["worker"] == args.worker]
    if args.error is not None:
        for result in results:
            result["errors"] = [
                e for e in result["errors"] if e["number"] == args.error
            ]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if not results:
        print(
            "No live progress checks found for this run attempt; counts are UNKNOWN, not zero."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()

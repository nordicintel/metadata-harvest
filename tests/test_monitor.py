import json

import pytest

from metadata_harvest.monitor import (
    CheckReporter,
    decode_errors,
    error_annotations,
    inspect_run,
)


@pytest.fixture(autouse=True)
def isolated_check_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_CHECK_POINTER", str(tmp_path / "check.json"))


def test_finalizer_closes_interrupted_check_without_overwriting_finished_result(
    tmp_path, monkeypatch
):
    from metadata_harvest.monitor import close_check

    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    (tmp_path / "check.json").write_text(
        json.dumps(
            {"path": "repos/owner/repo/check-runs/1", "external_id": "harvest:42:2:p"}
        )
    )
    calls = []
    state = {"status": "in_progress", "external_id": "harvest:42:2:p"}

    def api(path, **kwargs):
        calls.append(kwargs)
        return state

    close_check("cancelled", api=api)
    assert calls[-1]["payload"]["conclusion"] == "cancelled"
    state["status"] = "completed"
    calls.clear()
    close_check("failure", api=api)
    assert calls == [{}]


def test_large_individual_errors_remain_fully_inspectable():
    error = {
        "number": 1,
        "stage": "metadata",
        "resource": "T1",
        "language": "sv",
        "error": "å🦊" * 40000,
    }
    annotations = error_annotations([error])
    assert len(annotations) > 1
    assert all(len(a["message"].encode()) < 60000 for a in annotations)
    assert decode_errors(annotations) == [error]
    assert decode_errors(annotations[:-1])[0]["incomplete_details"] is True


def test_supervisor_stops_on_loss_of_visibility_without_restarting(
    monkeypatch, tmp_path
):
    from metadata_harvest import monitor

    class Reporter:
        def __init__(self, name):
            self.calls = 0

        def start(self):
            pass

        def update(self, *args, **kwargs):
            self.calls += 1
            raise OSError("GitHub unreachable")

    class Process:
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, **kwargs):
            return self.returncode

    process = Process()
    launches = []
    monkeypatch.setenv("HARVEST_PROGRESS", str(tmp_path / "progress.json"))
    monkeypatch.setattr(monitor, "CheckReporter", Reporter)
    monkeypatch.setattr(
        monitor.subprocess, "Popen", lambda *a, **kw: launches.append(a) or process
    )
    monkeypatch.setattr(monitor.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="stopping worker"):
        monitor.run_monitored("test", ["worker"], interval=0)
    assert process.terminated
    assert len(launches) == 1


def test_stale_heartbeat_cannot_look_healthy():
    def api(path, **kwargs):
        if "/actions/" in path:
            return {"head_sha": "abc", "run_attempt": 1, "status": "completed"}
        return [
            {
                "check_runs": [
                    {
                        "id": 1,
                        "external_id": "harvest:42:1:p",
                        "status": "in_progress",
                        "output": {
                            "text": json.dumps(
                                {
                                    "reported_at": "2020-01-01T00:00:00+00:00",
                                    "failed": 0,
                                }
                            )
                        },
                    }
                ]
            }
        ]

    result = inspect_run("owner/repo", "42", api=api)
    assert result[0]["stale"] is True


def test_annotation_outage_preserves_other_workers_and_skips_zero_errors():
    def api(path, **kwargs):
        if "/actions/" in path:
            return {"head_sha": "abc", "run_attempt": 1, "status": "in_progress"}
        if "/commits/" in path:
            return [
                {
                    "check_runs": [
                        {
                            "id": n,
                            "external_id": f"harvest:42:1:p{n}",
                            "status": "in_progress",
                            "output": {"text": json.dumps({"error_count": n})},
                        }
                        for n in (0, 1, 2)
                    ]
                }
            ]
        if "/1/annotations" in path:
            raise OSError("temporary GitHub error")
        assert "/2/annotations" in path
        return [[{"message": json.dumps({"number": 1, "error": "source failed"})}]]

    results = inspect_run("owner/repo", "42", errors=True, api=api)
    assert results[0]["errors"] == []
    assert "temporary GitHub error" in results[1]["errors_unavailable"]
    assert results[2]["errors"][0]["error"] == "source failed"


def test_live_check_publishes_all_errors_in_batches_and_read_verifies_start(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_SHA", "abc")
    calls = []

    def api(path, *, method="GET", payload=None, paginate=False):
        calls.append((path, method, payload))
        return {"id": 123, "external_id": "harvest:42:2:worker"}

    reporter = CheckReporter("worker", api=api)
    reporter.start()
    assert calls[-1][:2] == ("repos/owner/repo/check-runs/123", "GET")
    errors = [
        {
            "number": n,
            "stage": "metadata",
            "resource": f"T{n}",
            "language": "sv",
            "error": "HTTP 503",
        }
        for n in range(113)
    ]
    reporter.update({"processed": 120, "failed": 113, "succeeded": 7}, errors)
    updates = [payload for _, method, payload in calls if method == "PATCH"]
    assert [len(p["output"].get("annotations", [])) for p in updates] == [50, 50, 13]
    assert "T112" in updates[-1]["output"]["annotations"][-1]["message"]
    reporter.update({"processed": 120}, errors)
    assert not calls[-1][2]["output"].get("annotations")


def test_start_read_failure_prevents_worker_start(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_SHA", "abc")

    def api(path, **kwargs):
        if kwargs.get("method") == "POST":
            return {"id": 1}
        raise RuntimeError("Cannot inspect check")

    with pytest.raises(RuntimeError, match="Cannot inspect"):
        CheckReporter("worker", api=api).start()


def test_inspection_filters_run_attempt_and_fetches_individual_errors():
    def api(path, **kwargs):
        if "/actions/runs/" in path:
            return {"head_sha": "abc", "run_attempt": 2, "status": "in_progress"}
        if "/commits/" in path:
            return [
                {
                    "check_runs": [
                        {"id": 1, "external_id": "harvest:42:1:p", "output": {}},
                        {
                            "id": 2,
                            "external_id": "harvest:42:2:p",
                            "status": "in_progress",
                            "output": {"text": json.dumps({"failed": 1})},
                        },
                    ]
                }
            ]
        assert path.endswith("/2/annotations?per_page=100")
        return [
            [{"message": json.dumps({"number": 1, "resource": "T1", "error": "bad"})}]
        ]

    results = inspect_run("owner/repo", "42", errors=True, api=api)
    assert len(results) == 1
    assert results[0]["progress"]["failed"] == 1
    assert results[0]["errors"][0]["resource"] == "T1"

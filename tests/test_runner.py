"""The worker publishes a job. A separate process runs pytest."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from patchpilot.agents.errors import StopForLease
from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.runner.client import collect_test_report
from patchpilot.runner.errors import RunnerRejected, RunnerUnavailable
from patchpilot.runner.execute import _argv
from patchpilot.runner.limits import MAX_OUTPUT_CHARS

PASSING = "def test_ok():\n    print('suite-ok')\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"
SLOW = "def test_slow():\n    import time\n    time.sleep(30)\n"
LOUD = "def test_loud():\n    print('x' * 10000)\n    assert True\n"
ENV_PROBE = (
    "def test_env():\n"
    "    import os\n"
    "    print('HTTP_PROXY=' + os.environ.get('HTTP_PROXY', ''))\n"
    "    print('SENTINEL=' + os.environ.get('PATCHPILOT_SENTINEL', ''))\n"
    "    assert True\n"
)


def test_fixed_command_has_no_shell():
    project = Path("/runner/jobs/00000000-0000-0000-0000-000000000001/project")
    config = project.parent / "pytest.ini"
    argv = _argv(project, config)

    assert argv[:3] == (sys.executable, "-m", "pytest")
    assert "sh" not in argv
    assert "bash" not in argv
    assert "curl" not in argv
    assert argv[-1] == str(project)


def test_worker_client_does_not_start_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the worker started a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    worktree = _project(tmp_path / "work", PASSING)
    root = tmp_path / "runner"
    job_id = uuid4()

    with pytest.raises(RunnerUnavailable):
        collect_test_report(
            worktree=worktree,
            runner_root=root,
            job_id=job_id,
            wait_seconds=0.3,
        )

    request = json.loads((root / "requests" / f"{job_id}.json").read_text(encoding="utf-8"))
    assert set(request) == {"schema_version", "job_id"}
    assert request["job_id"] == str(job_id)
    assert worktree.joinpath("test_sample.py").read_text(encoding="utf-8") == PASSING


def test_runner_reports_a_passing_suite(tmp_path: Path):
    from tests.runner_support import start_runner, stop_runner

    root = tmp_path / "runner"
    worktree = _project(tmp_path / "work", PASSING)
    before = worktree.joinpath("test_sample.py").read_bytes()
    proc = start_runner(root, timeout=15)
    try:
        report = collect_test_report(
            worktree=worktree,
            runner_root=root,
            job_id=uuid4(),
            wait_seconds=15,
        )
    finally:
        stop_runner(proc)

    assert report.status.value == "passed"
    assert report.exit_code == 0
    assert report.timed_out is False
    assert "suite-ok" in report.stdout
    assert str(root) not in report.stdout
    assert worktree.joinpath("test_sample.py").read_bytes() == before


def test_runner_reports_a_failing_suite(tmp_path: Path):
    from tests.runner_support import start_runner, stop_runner

    root = tmp_path / "runner"
    proc = start_runner(root, timeout=15)
    try:
        report = collect_test_report(
            worktree=_project(tmp_path / "work", FAILING),
            runner_root=root,
            job_id=uuid4(),
            wait_seconds=15,
        )
    finally:
        stop_runner(proc)

    assert report.status.value == "failed"
    assert report.exit_code == 1
    assert report.timed_out is False
    assert "AssertionError" in report.stdout or "assert False" in report.stdout


def test_runner_reports_a_timeout(tmp_path: Path):
    from tests.runner_support import start_runner, stop_runner

    root = tmp_path / "runner"
    proc = start_runner(root, timeout=1)
    try:
        report = collect_test_report(
            worktree=_project(tmp_path / "work", SLOW),
            runner_root=root,
            job_id=uuid4(),
            wait_seconds=8,
        )
    finally:
        stop_runner(proc)

    assert report.status.value == "timed_out"
    assert report.timed_out is True
    assert report.exit_code is None


def test_runner_bounds_output(tmp_path: Path):
    from tests.runner_support import start_runner, stop_runner

    root = tmp_path / "runner"
    proc = start_runner(root, timeout=15)
    try:
        report = collect_test_report(
            worktree=_project(tmp_path / "work", LOUD),
            runner_root=root,
            job_id=uuid4(),
            wait_seconds=15,
        )
    finally:
        stop_runner(proc)

    assert report.status.value == "passed"
    assert report.output_truncated is True
    assert len(report.stdout) <= MAX_OUTPUT_CHARS


def test_a_request_cannot_choose_a_command(tmp_path: Path):
    from tests.runner_support import start_runner, stop_runner

    root = tmp_path / "runner"
    sentinel = tmp_path / "pwned"
    job_id = uuid4()
    root.mkdir()
    (root / "requests").mkdir()
    (root / "requests" / f"{job_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": str(job_id),
                "command": ["curl", "http://example.com"],
                "argv": ["/bin/sh", "-c", f"touch {sentinel}"],
            }
        ),
        encoding="utf-8",
    )
    proc = start_runner(root, timeout=5)
    try:
        report = _wait_for_result(root, job_id)
    finally:
        stop_runner(proc)

    assert report["status"] == "invalid_request"
    assert not sentinel.exists()


def test_runner_refuses_a_secret_environment(tmp_path: Path):
    root = tmp_path / "runner"
    root.mkdir()
    env = {
        "PATH": "/usr/bin",
        "RUNNER_ROOT": str(root),
        "DATABASE_URL": "postgresql://patchpilot:db-password-sentinel@db/patchpilot",
        "HTTP_PROXY": "http://proxy.invalid",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "patchpilot.runner"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "runner refused secret environment" in completed.stderr
    assert "db-password-sentinel" not in completed.stderr
    assert "db-password-sentinel" not in completed.stdout


def test_pytest_child_does_not_receive_proxy_or_sentinel(tmp_path: Path):
    from tests.runner_support import start_runner, stop_runner

    root = tmp_path / "runner"
    proc = start_runner(
        root,
        timeout=15,
        extra_env={
            "HTTP_PROXY": "http://proxy.invalid",
            "PATCHPILOT_SENTINEL": "runner-sentinel",
        },
    )
    try:
        report = collect_test_report(
            worktree=_project(tmp_path / "work", ENV_PROBE),
            runner_root=root,
            job_id=uuid4(),
            wait_seconds=15,
        )
    finally:
        stop_runner(proc)

    assert report.status.value == "passed"
    assert "HTTP_PROXY=" in report.stdout
    assert "proxy.invalid" not in report.stdout
    assert "runner-sentinel" not in report.stdout
    assert "SENTINEL=" in report.stdout


def test_symlink_is_not_copied_into_the_runner(tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("leave-me", encoding="utf-8")
    worktree = _project(tmp_path / "work", PASSING)
    (worktree / "leak.txt").symlink_to(outside)

    with pytest.raises(RunnerRejected):
        collect_test_report(
            worktree=worktree,
            runner_root=tmp_path / "runner",
            job_id=uuid4(),
            wait_seconds=1,
        )

    assert outside.read_text(encoding="utf-8") == "leave-me"
    assert not (tmp_path / "runner" / "requests").exists() or not any(
        (tmp_path / "runner" / "requests").glob("*.json")
    )


def test_a_lost_lease_stops_the_wait(tmp_path: Path):
    with pytest.raises(StopForLease):
        collect_test_report(
            worktree=_project(tmp_path / "work", PASSING),
            runner_root=tmp_path / "runner",
            job_id=uuid4(),
            wait_seconds=5,
            lease_lost=lambda: True,
        )


def test_missing_test_report_is_404(
    postgres_database_url: str,
    clean_tasks: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("ARTIFACTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        missing = client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000/tests")
        assert missing.status_code == 404
        created = client.post(
            "/api/v1/tasks",
            json={"title": "No tests yet", "description": "The suite has not run."},
        )
        assert created.status_code == 201
        listed = client.get(f"/api/v1/tasks/{created.json()['id']}/tests")
        assert listed.status_code == 404


def _project(root: Path, source: str) -> Path:
    root.mkdir()
    (root / "test_sample.py").write_text(source, encoding="utf-8")
    return root


def _wait_for_result(root: Path, job_id: object) -> dict[str, object]:
    import time

    path = root / "results" / f"{job_id}.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(loaded, dict)
            return loaded
        time.sleep(0.05)
    raise AssertionError("runner did not write a result")

"""Worker side of the runner protocol.

This module copies a worktree and waits for a result file. It does not start
pytest and it does not create a process. The runner process is a separate
Compose service.
"""

import math
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid5

from patchpilot.agents.errors import StopForLease
from patchpilot.runner.errors import RunnerRejected, RunnerUnavailable
from patchpilot.runner.limits import MAX_RUNNER_WAIT_SECONDS
from patchpilot.runner.protocol import (
    TestReport,
    TestStatus,
    prepare_runner_root,
    read_report,
    redact,
)
from patchpilot.runner.staging import remove_project, stage_project

# Attempt 0 keeps the task id so a resumed first test reuses its result file.
_ATTEMPT_NAMESPACE = UUID("c0ffee00-0000-4000-8000-000000000014")


def test_job_id(task_id: UUID, repair_count: int) -> UUID:
    """Job id for one test pass. Repair passes do not reuse the first result."""
    if not isinstance(task_id, UUID):
        raise RunnerRejected
    if isinstance(repair_count, bool) or not isinstance(repair_count, int):
        raise RunnerRejected
    if repair_count == 0:
        return task_id
    if not 1 <= repair_count <= 2:
        raise RunnerRejected
    return uuid5(_ATTEMPT_NAMESPACE, f"{task_id}:{repair_count}")


def baseline_job_id(task_id: UUID) -> UUID:
    """Job id for the unmodified worktree. A patched pass does not reuse it."""
    if not isinstance(task_id, UUID):
        raise RunnerRejected
    return uuid5(_ATTEMPT_NAMESPACE, f"{task_id}:baseline")


def collect_test_report(
    *,
    worktree: Path,
    runner_root: Path,
    job_id: UUID,
    wait_seconds: float,
    lease_lost: Callable[[], bool] | None = None,
) -> TestReport:
    """Publish one job and return the runner's report.

    `job_id` selects the result file. A report that is already stored for that
    id is reused, so a resumed node does not ask for a second run. A repair
    passes a different id from `test_job_id`.
    """
    _check_lease(lease_lost)
    wait = _wait(wait_seconds)
    try:
        base = prepare_runner_root(runner_root, create=True)
    except ValueError:
        raise RunnerRejected from None
    existing = _finished(base, job_id)
    if existing is not None:
        return existing
    stage_project(worktree, base, job_id)
    _check_lease(lease_lost)
    deadline = time.monotonic() + wait
    while True:
        _check_lease(lease_lost)
        found = _finished(base, job_id)
        if found is not None:
            return found
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunnerUnavailable
        time.sleep(min(0.05, remaining))


def _finished(root: Path, job_id: UUID) -> TestReport | None:
    try:
        report = read_report(root, job_id)
    except ValueError:
        raise RunnerRejected from None
    if report is None:
        return None
    if report.status == TestStatus.INVALID_REQUEST:
        raise RunnerRejected
    remove_project(root, job_id)
    return report.model_copy(
        update={
            "stdout": redact(report.stdout, (root,)),
            "stderr": redact(report.stderr, (root,)),
        }
    )


def _wait(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerRejected
    wait = float(value)
    if not math.isfinite(wait) or wait <= 0 or wait > MAX_RUNNER_WAIT_SECONDS:
        raise RunnerRejected
    return wait


def _check_lease(lease_lost: Callable[[], bool] | None) -> None:
    if lease_lost is not None and lease_lost():
        raise StopForLease

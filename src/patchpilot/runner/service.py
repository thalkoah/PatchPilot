"""Poll the shared volume and run the fixed pytest command.

One job at a time. A request that is not the published schema is recorded as
invalid and is not executed. The result file is the only thing the worker reads.
"""

import logging
import time
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from patchpilot.runner.execute import run_pytest
from patchpilot.runner.protocol import (
    invalid_report,
    job_id_from_filename,
    prepare_runner_root,
    project_dir,
    read_request,
    request_path,
    result_path,
    write_report,
)
from patchpilot.source.paths import is_inside

logger = logging.getLogger(__name__)


def serve(root: Path, *, timeout_seconds: float, poll_seconds: float) -> None:
    """Handle published jobs until the process is interrupted."""
    base = prepare_runner_root(root, create=True)
    logger.info("runner started")
    while True:
        try:
            worked = run_pending(base, timeout_seconds=timeout_seconds)
        except Exception as exc:
            logger.error("runner iteration failed: %s", type(exc).__name__)
            worked = False
        if not worked:
            time.sleep(poll_seconds)


def run_pending(root: Path, *, timeout_seconds: float) -> bool:
    """Run the oldest unpublished result. Return False when nothing is waiting."""
    base = prepare_runner_root(root, create=True)
    job_id = _next(base)
    if job_id is None:
        return False
    _handle(base, job_id, timeout_seconds)
    return True


def _next(root: Path) -> UUID | None:
    directory = root / "requests"
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        names = sorted(
            path.name for path in directory.iterdir() if path.is_file() and not path.is_symlink()
        )
    except OSError:
        return None
    for name in names:
        job_id = job_id_from_filename(name)
        if job_id is None:
            continue
        finished = result_path(root, job_id)
        try:
            if finished.is_symlink():
                continue
            if finished.exists():
                continue
        except OSError:
            continue
        return job_id
    return None


def _handle(root: Path, job_id: UUID, timeout_seconds: float) -> None:
    path = request_path(root, job_id)
    try:
        if path.is_symlink():
            write_report(root, invalid_report(job_id))
            logger.info("runner job=%s status=invalid_request", job_id)
            return
        request = read_request(root, job_id)
    except (OSError, ValidationError, ValueError):
        write_report(root, invalid_report(job_id))
        logger.info("runner job=%s status=invalid_request", job_id)
        return
    if request.job_id != job_id:
        write_report(root, invalid_report(job_id))
        logger.info("runner job=%s status=invalid_request", job_id)
        return
    try:
        project = project_dir(root, job_id)
        ready = not project.is_symlink() and project.is_dir() and is_inside(root, project)
    except (OSError, ValueError):
        ready = False
    if not ready:
        write_report(root, invalid_report(job_id))
        logger.info("runner job=%s status=invalid_request", job_id)
        return
    try:
        report = run_pytest(
            project,
            job_id=job_id,
            timeout_seconds=timeout_seconds,
            runner_root=root,
        )
    except (OSError, ValueError):
        write_report(root, invalid_report(job_id))
        logger.info("runner job=%s status=invalid_request", job_id)
        return
    write_report(root, report)
    logger.info("runner job=%s status=%s", job_id, report.status.value)

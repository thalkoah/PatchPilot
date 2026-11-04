"""The only place PatchPilot starts pytest.

The command is a fixed list. The working directory is the job's project
directory. Nothing in the request can change the executable or the arguments.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID

from patchpilot.runner.env import pytest_child_env
from patchpilot.runner.limits import (
    MAX_CAPTURE_BYTES,
    MAX_DRAIN_BYTES,
    MAX_OUTPUT_CHARS,
    MAX_RUNNER_TIMEOUT_SECONDS,
)
from patchpilot.runner.protocol import (
    TestReport,
    TestStatus,
    config_path,
    redact,
)

# The runner writes this file beside the project. A pytest.ini inside the
# copied tree cannot add arguments, because -c points here.
_PYTEST_CONFIG = "[pytest]\naddopts =\n"


def run_pytest(
    project: Path, *, job_id: UUID, timeout_seconds: float, runner_root: Path
) -> TestReport:
    """Run the fixed command. `project` is the staged job directory."""
    timeout = _timeout(timeout_seconds)
    config = config_path(runner_root, job_id)
    _write_config(config)
    argv = _argv(project, config)
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=project,
        env=pytest_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    code, timed_out, stdout, stderr, truncated = _capture(proc, timeout)
    duration_ms = min(int(max(0.0, time.monotonic() - started) * 1000), 3_600_000)
    text_out, cut_out = _text(stdout)
    text_err, cut_err = _text(stderr)
    text_out = redact(text_out, (project, runner_root, config.parent))
    text_err = redact(text_err, (project, runner_root, config.parent))
    limited = truncated or cut_out or cut_err
    if timed_out:
        status = TestStatus.TIMED_OUT
        exit_code = None
    elif code == 0:
        status = TestStatus.PASSED
        exit_code = 0
    else:
        status = TestStatus.FAILED
        exit_code = code if isinstance(code, int) and 0 < code <= 255 else 1
    return TestReport(
        job_id=job_id,
        status=status,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
        stdout=text_out,
        stderr=text_err,
        output_truncated=limited,
    )


def _argv(project: Path, config: Path) -> tuple[str, ...]:
    """Fixed pytest invocation. The two paths are the staged job, not a request field."""
    return (
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(config),
        "--rootdir",
        str(project),
        "-p",
        "no:cacheprovider",
        "--tb=line",
        "-q",
        "-s",
        str(project),
    )


def _write_config(path: Path) -> None:
    if path.is_symlink():
        raise OSError
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PYTEST_CONFIG, encoding="utf-8")


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout is invalid")
    timeout = float(value)
    if timeout <= 0 or timeout > MAX_RUNNER_TIMEOUT_SECONDS:
        raise ValueError("timeout is invalid")
    return timeout


def _capture(
    proc: subprocess.Popen[bytes],
    timeout: float,
) -> tuple[int | None, bool, bytes, bytes, bool]:
    stdout_pipe = proc.stdout
    stderr_pipe = proc.stderr
    if stdout_pipe is None or stderr_pipe is None:
        _kill(proc)
        return 1, False, b"", b"", True
    captured: dict[str, tuple[bytes, bool]] = {}

    def take(name: str, pipe: object) -> None:
        captured[name] = _drain(pipe)

    threads = (
        threading.Thread(target=take, args=("out", stdout_pipe)),
        threading.Thread(target=take, args=("err", stderr_pipe)),
    )
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
    for thread in threads:
        thread.join(timeout=2)
    if proc.poll() is None:
        _kill(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            timed_out = True
    stdout, out_cut = captured.get("out", (b"", True))
    stderr, err_cut = captured.get("err", (b"", True))
    code = None if timed_out else proc.returncode
    return code, timed_out, stdout, stderr, out_cut or err_cut


def _drain(pipe: object) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    kept = 0
    seen = 0
    truncated = False
    while True:
        block = pipe.read(8192)  # type: ignore[attr-defined]
        if not block:
            break
        seen += len(block)
        if kept < MAX_CAPTURE_BYTES:
            room = MAX_CAPTURE_BYTES - kept
            chunks.append(block[:room])
            kept += min(len(block), room)
        if seen > MAX_CAPTURE_BYTES:
            truncated = True
        if seen >= MAX_DRAIN_BYTES:
            truncated = True
            pipe.close()  # type: ignore[attr-defined]
            break
    return b"".join(chunks), truncated


def _text(payload: bytes) -> tuple[str, bool]:
    text = payload.decode("utf-8", errors="replace")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[:MAX_OUTPUT_CHARS], True


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()

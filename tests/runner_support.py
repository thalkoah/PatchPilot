"""Start the restricted runner as its own process."""

import signal
import subprocess
import sys
from pathlib import Path

from patchpilot.runner.env import runner_process_env


def start_runner(
    root: Path,
    *,
    timeout: float = 15,
    poll: float = 0.05,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Run `python -m patchpilot.runner` with a scrubbed environment."""
    root.mkdir(parents=True, exist_ok=True)
    env = runner_process_env(root=root, timeout_seconds=timeout, poll_seconds=poll)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "patchpilot.runner"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def stop_runner(proc: subprocess.Popen[bytes]) -> str:
    """Stop the runner and return its stderr. The text is for a failed assertion."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    stderr = b"" if proc.stderr is None else proc.stderr.read()
    return stderr.decode("utf-8", errors="replace")

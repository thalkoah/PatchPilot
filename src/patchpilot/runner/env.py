"""Environment for the runner process and the pytest child.

The allowlist is the credential boundary. Database settings, model settings,
and proxy variables are not copied. The runner refuses to start when a secret
is already in its own environment, so a misconfigured container stops before
pytest runs.
"""

import os
from collections.abc import Mapping
from pathlib import Path

_PASS_THROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HOME",
    "TMPDIR",
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
)

_SECRET_NAMES = frozenset(
    {
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "OLLAMA_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    }
)

_SECRET_PARTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "database_url",
)


def secret_environment(environ: dict[str, str] | None = None) -> bool:
    """True when this process was given a credential or a model setting."""
    source = os.environ if environ is None else environ
    for name in source:
        folded = name.casefold()
        if name in _SECRET_NAMES or any(part in folded for part in _SECRET_PARTS):
            return True
    return False


def runner_process_env(
    *,
    root: Path,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, str]:
    """Environment for a runner process started by a test or an operator.

    Compose sets the same keys itself and does not use this helper. The helper
    exists so a host test does not leak the parent process environment.
    """
    env = _allowlisted(os.environ)
    env["RUNNER_ROOT"] = str(root)
    env["RUNNER_TIMEOUT_SECONDS"] = str(timeout_seconds)
    env["RUNNER_POLL_SECONDS"] = str(poll_seconds)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("HOME", "/tmp")
    return env


def pytest_child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the pytest process. Runner paths and secrets stay out."""
    source = os.environ if environ is None else environ
    env = _allowlisted(source)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("HOME", "/tmp")
    return env


def _allowlisted(source: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in _PASS_THROUGH:
        value = source.get(name)
        if value is None or value == "" or "\x00" in value or "\n" in value:
            continue
        env[name] = value
    return env

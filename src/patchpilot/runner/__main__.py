"""Restricted pytest runner.

Start this with `python -m patchpilot.runner`. It must not be given the
database password or model settings. Compose runs it as its own service with
no network, and the API and worker do not import the pytest entrypoint.
"""

import logging
import signal
import sys
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from patchpilot.runner.env import secret_environment
from patchpilot.runner.limits import (
    DEFAULT_RUNNER_POLL_SECONDS,
    DEFAULT_RUNNER_TIMEOUT_SECONDS,
    MAX_RUNNER_POLL_SECONDS,
    MAX_RUNNER_TIMEOUT_SECONDS,
)
from patchpilot.runner.service import serve


class RunnerSettings(BaseSettings):
    """Runner process settings. This loader does not read `.env` or the database URL."""

    model_config = SettingsConfigDict(extra="ignore", env_file=None)

    runner_root: str
    runner_timeout_seconds: float = Field(
        default=DEFAULT_RUNNER_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_RUNNER_TIMEOUT_SECONDS,
    )
    runner_poll_seconds: float = Field(
        default=DEFAULT_RUNNER_POLL_SECONDS,
        gt=0,
        le=MAX_RUNNER_POLL_SECONDS,
    )

    @field_validator("runner_root")
    @classmethod
    def runner_root_is_absolute(cls, value: str) -> str:
        path = Path(value.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError("runner_root must be an absolute path")
        return str(path)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if secret_environment():
        print("runner refused secret environment", file=sys.stderr)
        raise SystemExit(2)
    try:
        settings = RunnerSettings()
    except ValidationError:
        print("invalid configuration", file=sys.stderr)
        raise SystemExit(2) from None
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    try:
        serve(
            Path(settings.runner_root),
            timeout_seconds=settings.runner_timeout_seconds,
            poll_seconds=settings.runner_poll_seconds,
        )
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("runner stopped")


def _stop_on_sigterm(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()

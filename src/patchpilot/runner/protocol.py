"""Job files on the shared runner volume.

A request is only a schema version and a job id. The runner chooses the
pytest command and the project directory from that id. A path, an executable,
or an argument in the file is rejected and is not executed.

The volume is shared by every job. That is weaker than mounting one workspace
into a fresh container per task. The worker concurrency stays 1.
"""

import json
import os
import re
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from patchpilot.runner.limits import MAX_OUTPUT_CHARS, SCHEMA_VERSION
from patchpilot.source.paths import is_inside

_JOB = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TestStatus(StrEnum):
    """What the runner recorded. `invalid_request` means pytest did not start."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INVALID_REQUEST = "invalid_request"


class JobRequest(BaseModel):
    """The only document the worker may publish."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION)
    job_id: UUID

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_current(cls, value: int) -> int:
        if isinstance(value, bool) or value != SCHEMA_VERSION:
            raise ValueError("schema_version is not supported")
        return value


class TestReport(BaseModel):
    """One pytest outcome. Paths from the runner volume are not included."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    job_id: UUID
    status: TestStatus
    exit_code: int | None = None
    timed_out: bool
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_current(cls, value: int) -> int:
        if isinstance(value, bool) or value != SCHEMA_VERSION:
            raise ValueError("schema_version is not supported")
        return value

    @field_validator("exit_code")
    @classmethod
    def exit_code_is_a_status(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError("exit_code must be from 0 to 255")
        return value

    @field_validator("duration_ms")
    @classmethod
    def duration_is_bounded(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3_600_000:
            raise ValueError("duration_ms is out of range")
        return value

    @field_validator("stdout", "stderr")
    @classmethod
    def output_is_bounded(cls, value: str) -> str:
        if "\x00" in value or len(value) > MAX_OUTPUT_CHARS:
            raise ValueError("output is too large")
        return value

    @model_validator(mode="after")
    def status_matches_the_exit(self) -> "TestReport":
        if self.status == TestStatus.PASSED:
            if self.exit_code != 0 or self.timed_out:
                raise ValueError("a passing report needs exit code 0")
        elif self.status == TestStatus.FAILED:
            if self.timed_out or self.exit_code in {None, 0}:
                raise ValueError("a failing report needs a non-zero exit code")
        elif self.status == TestStatus.TIMED_OUT:
            if not self.timed_out or self.exit_code is not None:
                raise ValueError("a timeout report has no exit code")
        elif self.status == TestStatus.INVALID_REQUEST:
            if (
                self.exit_code is not None
                or self.timed_out
                or self.stdout
                or self.stderr
                or self.output_truncated
            ):
                raise ValueError("an invalid request has no output")
        return self


def prepare_runner_root(root: Path, *, create: bool = True) -> Path:
    """Return the absolute runner directory. A symlink root is refused."""
    if not isinstance(root, Path):
        raise ValueError("runner root is missing")
    try:
        if root.is_symlink():
            raise ValueError("runner root is missing")
        resolved = root.expanduser().resolve()
    except (OSError, ValueError):
        raise ValueError("runner root is missing") from None
    if not resolved.is_absolute():
        raise ValueError("runner root is missing")
    if create:
        try:
            resolved.mkdir(parents=True, exist_ok=True)
            for name in ("requests", "results", "jobs"):
                (resolved / name).mkdir(exist_ok=True)
        except OSError:
            raise ValueError("runner root is missing") from None
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("runner root is missing")
    return resolved


def request_path(root: Path, job_id: UUID) -> Path:
    return _file(root, "requests", job_id)


def result_path(root: Path, job_id: UUID) -> Path:
    return _file(root, "results", job_id)


def project_dir(root: Path, job_id: UUID) -> Path:
    """The only directory pytest may use for this job."""
    base = prepare_runner_root(root, create=False)
    candidate = base / "jobs" / str(job_id) / "project"
    if not is_inside(base, candidate.parent.parent) or not is_inside(base, candidate.parent):
        raise ValueError("runner root is missing")
    return candidate


def config_path(root: Path, job_id: UUID) -> Path:
    """Runner-owned pytest config. It is not inside the copied project."""
    base = prepare_runner_root(root, create=False)
    candidate = base / "jobs" / str(job_id) / "pytest.ini"
    if not is_inside(base, candidate.parent):
        raise ValueError("runner root is missing")
    return candidate


def job_id_from_filename(name: str) -> UUID | None:
    """Accept only `<uuid>.json`. Anything else is ignored."""
    if not isinstance(name, str) or not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    if _JOB.fullmatch(stem) is None:
        return None
    return UUID(stem)


def write_request(root: Path, job_id: UUID) -> None:
    """Publish one job. The file has no command and no path."""
    base = prepare_runner_root(root, create=True)
    payload = _dump(JobRequest(schema_version=SCHEMA_VERSION, job_id=job_id))
    _atomic(request_path(base, job_id), payload)


def read_request(root: Path, job_id: UUID) -> JobRequest:
    """Load a request. Extra fields, including a command, raise ValidationError."""
    path = request_path(prepare_runner_root(root, create=False), job_id)
    return JobRequest.model_validate(_load(path))


def write_report(root: Path, report: TestReport) -> None:
    """Write a result once. The same bytes may already be there."""
    base = prepare_runner_root(root, create=True)
    payload = report_bytes(report)
    path = result_path(base, report.job_id)
    try:
        if path.is_symlink():
            raise ValueError("runner root is missing")
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("runner root is missing")
            return
    except ValueError:
        raise
    except OSError:
        raise ValueError("runner root is missing") from None
    _atomic(path, payload)


def read_report(root: Path, job_id: UUID) -> TestReport | None:
    """Return a finished report, or None when the runner has not written one."""
    path = result_path(prepare_runner_root(root, create=False), job_id)
    try:
        if path.is_symlink() or not path.exists():
            return None
        if not path.is_file():
            raise ValueError("runner root is missing")
    except ValueError:
        raise
    except OSError:
        raise ValueError("runner root is missing") from None
    try:
        report = TestReport.model_validate(_load(path))
    except (ValidationError, json.JSONDecodeError, OSError, UnicodeError, ValueError):
        raise ValueError("runner root is missing") from None
    if report.job_id != job_id:
        raise ValueError("runner root is missing")
    return report


def report_bytes(report: TestReport) -> bytes:
    return _dump(report)


def invalid_report(job_id: UUID) -> TestReport:
    """A refusal. The body of the bad request is not copied into the result."""
    return TestReport(
        schema_version=SCHEMA_VERSION,
        job_id=job_id,
        status=TestStatus.INVALID_REQUEST,
        exit_code=None,
        timed_out=False,
        duration_ms=0,
        stdout="",
        stderr="",
        output_truncated=False,
    )


def redact(text: str, roots: tuple[Path, ...]) -> str:
    """Replace runner paths before the report leaves the runner volume."""
    redacted = text
    for root in roots:
        redacted = redacted.replace(str(root), ".")
        try:
            redacted = redacted.replace(str(root.resolve()), ".")
        except OSError:
            continue
    return redacted


def _file(root: Path, directory: str, job_id: UUID) -> Path:
    base = prepare_runner_root(root, create=False)
    candidate = base / directory / f"{job_id}.json"
    if not is_inside(base, candidate.parent) or candidate.parent.name != directory:
        raise ValueError("runner root is missing")
    return candidate


def _dump(model: BaseModel) -> bytes:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()


def _load(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError("runner root is missing")
    return json.loads(path.read_bytes())


def _atomic(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("runner root is missing")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    except OSError:
        raise ValueError("runner root is missing") from None
    finally:
        if temporary.exists():
            temporary.unlink()

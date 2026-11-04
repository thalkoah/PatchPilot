"""Artifact files. The database stores the checksum, not the body.

The relative path is chosen here from the task id and version. Model output never
selects it. A plan file is JSON with `schema_version` 1 and a `ChangePlan`.
A proposal file is the model `PatchProposal`. A diff file is the unified diff
Git applied, which is a different byte string from the proposal.
"""

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

from patchpilot.llm.patch import PatchProposal
from patchpilot.llm.plan import ChangePlan
from patchpilot.runner.limits import MAX_REPORT_BYTES
from patchpilot.runner.protocol import TestReport, report_bytes
from patchpilot.source.paths import is_inside

PLAN_ARTIFACT_TYPE = "plan"
DIFF_ARTIFACT_TYPE = "diff"
PROPOSAL_ARTIFACT_TYPE = "proposal"
TEST_REPORT_ARTIFACT_TYPE = "test-report"
PLAN_SCHEMA_VERSION = 1
PATCH_SCHEMA_VERSION = 1
TEST_REPORT_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 32_768
MAX_DIFF_BYTES = 16_384
MAX_PLAN_VERSION = 1000

_TASK = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_PLAN_FILE = re.compile(rf"^{_TASK}/plan/v([1-9][0-9]{{0,3}})\.json$")
_DIFF_FILE = re.compile(rf"^{_TASK}/diff/v([1-9][0-9]{{0,3}})\.patch$")
_PROPOSAL_FILE = re.compile(rf"^{_TASK}/proposal/v([1-9][0-9]{{0,3}})\.json$")
_REPORT_FILE = re.compile(rf"^{_TASK}/test-report/v([1-9][0-9]{{0,3}})\.json$")


class ArtifactStoreError(Exception):
    """The plan file could not be written or did not match its stored checksum."""


class TestReportStoreError(ArtifactStoreError):
    """The test report file could not be written or did not match its checksum."""


def plan_document(plan: ChangePlan) -> bytes:
    """Canonical JSON for one plan. The checksum is the digest of these bytes."""
    body = {"plan": plan.model_dump(mode="json"), "schema_version": PLAN_SCHEMA_VERSION}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_PLAN_BYTES:
        raise ArtifactStoreError
    return payload


def parse_plan_document(payload: bytes) -> ChangePlan:
    """Validate a plan file. The error does not include the file body."""
    if not isinstance(payload, bytes) or len(payload) > MAX_PLAN_BYTES:
        raise ArtifactStoreError
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise ArtifactStoreError from None
    if not isinstance(data, dict) or data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ArtifactStoreError
    try:
        return ChangePlan.model_validate(data.get("plan"))
    except ValueError:
        raise ArtifactStoreError from None


def proposal_document(proposal: PatchProposal) -> bytes:
    """Canonical JSON for the model proposal. This is not the applied diff."""
    body = {
        "proposal": proposal.model_dump(mode="json"),
        "schema_version": PATCH_SCHEMA_VERSION,
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_PLAN_BYTES:
        raise ArtifactStoreError
    return payload


def parse_proposal_document(payload: bytes) -> PatchProposal:
    """Validate a proposal file. The error does not include the file body."""
    if not isinstance(payload, bytes) or len(payload) > MAX_PLAN_BYTES:
        raise ArtifactStoreError
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise ArtifactStoreError from None
    if not isinstance(data, dict) or data.get("schema_version") != PATCH_SCHEMA_VERSION:
        raise ArtifactStoreError
    try:
        return PatchProposal.model_validate(data.get("proposal"))
    except ValueError:
        raise ArtifactStoreError from None


def plan_relative_path(task_id: UUID, version: int) -> str:
    """Internal path for one plan revision. It is not taken from the model."""
    return _versioned_path(task_id, version, "plan", "json", _PLAN_FILE)


def diff_relative_path(task_id: UUID, version: int) -> str:
    """Internal path for the applied diff. It is not taken from the model."""
    return _versioned_path(task_id, version, "diff", "patch", _DIFF_FILE)


def proposal_relative_path(task_id: UUID, version: int) -> str:
    """Internal path for the model proposal. It is not taken from the model."""
    return _versioned_path(task_id, version, "proposal", "json", _PROPOSAL_FILE)


def test_report_relative_path(task_id: UUID, version: int) -> str:
    """Internal path for one test report. It is not taken from the model."""
    return _versioned_path(task_id, version, "test-report", "json", _REPORT_FILE)


def test_report_document(report: TestReport) -> bytes:
    """Canonical JSON for one runner report. The checksum is the digest of these bytes."""
    payload = report_bytes(report)
    if len(payload) > MAX_REPORT_BYTES:
        raise TestReportStoreError
    return payload


def parse_test_report_document(payload: bytes) -> TestReport:
    """Validate a test report file. The error does not include the file body."""
    if not isinstance(payload, bytes) or len(payload) > MAX_REPORT_BYTES:
        raise TestReportStoreError
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise TestReportStoreError from None
    try:
        return TestReport.model_validate(data)
    except ValueError:
        raise TestReportStoreError from None


def _versioned_path(
    task_id: UUID,
    version: int,
    kind: str,
    suffix: str,
    pattern: re.Pattern[str],
) -> str:
    if isinstance(version, bool) or not isinstance(version, int):
        raise ArtifactStoreError
    if not 1 <= version <= MAX_PLAN_VERSION:
        raise ArtifactStoreError
    relative = f"{task_id}/{kind}/v{version}.{suffix}"
    if pattern.fullmatch(relative) is None:
        raise ArtifactStoreError
    return relative


def checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def prepare_artifact_root(root: Path, *, create: bool = True) -> Path:
    """Return the absolute artifact directory. Reads do not create it."""
    if not isinstance(root, Path):
        raise ArtifactStoreError
    try:
        resolved = root.expanduser().resolve()
    except OSError:
        raise ArtifactStoreError from None
    if not resolved.is_absolute():
        raise ArtifactStoreError
    if create:
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ArtifactStoreError from None
    if not resolved.is_dir():
        raise ArtifactStoreError
    return resolved


def write_plan_file(root: Path, relative_path: str, payload: bytes) -> None:
    """Write a plan file once. The same bytes may already be there. Different bytes are refused."""
    _write_file(root, relative_path, payload, _PLAN_FILE, MAX_PLAN_BYTES)


def write_diff_file(root: Path, relative_path: str, payload: bytes) -> None:
    """Write the applied diff once. The same bytes may already be there."""
    _write_file(root, relative_path, payload, _DIFF_FILE, MAX_DIFF_BYTES)


def write_proposal_file(root: Path, relative_path: str, payload: bytes) -> None:
    """Write the model proposal once. The same bytes may already be there."""
    _write_file(root, relative_path, payload, _PROPOSAL_FILE, MAX_PLAN_BYTES)


def write_test_report_file(root: Path, relative_path: str, payload: bytes) -> None:
    """Write a test report once. The same bytes may already be there."""
    try:
        _write_file(root, relative_path, payload, _REPORT_FILE, MAX_REPORT_BYTES)
    except ArtifactStoreError as exc:
        raise TestReportStoreError from exc


def _write_file(
    root: Path,
    relative_path: str,
    payload: bytes,
    pattern: re.Pattern[str],
    limit: int,
) -> None:
    if len(payload) > limit or not payload:
        raise ArtifactStoreError
    path = _destination(root, relative_path, pattern, create=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ArtifactStoreError
        if path.exists():
            if not path.is_file() or path.read_bytes() != payload:
                raise ArtifactStoreError
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except ArtifactStoreError:
        raise
    except OSError:
        raise ArtifactStoreError from None


def load_plan_file(
    root: Path,
    *,
    relative_path: str,
    expected_checksum: str,
    size: int,
) -> ChangePlan:
    """Read a plan file only when its size and checksum match the artifact row."""
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not isinstance(expected_checksum, str)
        or len(expected_checksum) != 64
    ):
        raise ArtifactStoreError
    path = _destination(root, relative_path, _PLAN_FILE, create=False)
    try:
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError
        payload = path.read_bytes()
    except ArtifactStoreError:
        raise
    except OSError:
        raise ArtifactStoreError from None
    if len(payload) != size or checksum(payload) != expected_checksum:
        raise ArtifactStoreError
    return parse_plan_document(payload)


def load_diff_file(
    root: Path,
    *,
    relative_path: str,
    expected_checksum: str,
    size: int,
) -> bytes:
    """Read an applied diff only when its size and checksum match the artifact row."""
    payload = _read_file(
        root,
        relative_path=relative_path,
        pattern=_DIFF_FILE,
        expected_checksum=expected_checksum,
        size=size,
        limit=MAX_DIFF_BYTES,
    )
    if not payload.startswith(b"diff --git "):
        raise ArtifactStoreError
    return payload


def load_test_report_file(
    root: Path,
    *,
    relative_path: str,
    expected_checksum: str,
    size: int,
) -> TestReport:
    """Read a test report only when its size and checksum match the artifact row."""
    try:
        payload = _read_file(
            root,
            relative_path=relative_path,
            pattern=_REPORT_FILE,
            expected_checksum=expected_checksum,
            size=size,
            limit=MAX_REPORT_BYTES,
        )
    except ArtifactStoreError as exc:
        raise TestReportStoreError from exc
    return parse_test_report_document(payload)


def load_proposal_file(
    root: Path,
    *,
    relative_path: str,
    expected_checksum: str,
    size: int,
) -> PatchProposal:
    """Read a model proposal only when its size and checksum match the artifact row."""
    payload = _read_file(
        root,
        relative_path=relative_path,
        pattern=_PROPOSAL_FILE,
        expected_checksum=expected_checksum,
        size=size,
        limit=MAX_PLAN_BYTES,
    )
    return parse_proposal_document(payload)


def _read_file(
    root: Path,
    *,
    relative_path: str,
    pattern: re.Pattern[str],
    expected_checksum: str,
    size: int,
    limit: int,
) -> bytes:
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not isinstance(expected_checksum, str)
        or len(expected_checksum) != 64
        or size > limit
    ):
        raise ArtifactStoreError
    path = _destination(root, relative_path, pattern, create=False)
    try:
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError
        payload = path.read_bytes()
    except ArtifactStoreError:
        raise
    except OSError:
        raise ArtifactStoreError from None
    if len(payload) != size or checksum(payload) != expected_checksum:
        raise ArtifactStoreError
    return payload


def _destination(
    root: Path,
    relative_path: str,
    pattern: re.Pattern[str],
    *,
    create: bool,
) -> Path:
    if pattern.fullmatch(relative_path) is None:
        raise ArtifactStoreError
    base = prepare_artifact_root(root, create=create)
    candidate = base.joinpath(*relative_path.split("/"))
    try:
        if candidate.is_symlink():
            raise ArtifactStoreError
        parent = candidate.parent.resolve()
    except OSError:
        raise ArtifactStoreError from None
    if not is_inside(base, parent):
        raise ArtifactStoreError
    if candidate.exists():
        try:
            resolved = candidate.resolve()
        except OSError:
            raise ArtifactStoreError from None
        if not is_inside(base, resolved):
            raise ArtifactStoreError
    return candidate

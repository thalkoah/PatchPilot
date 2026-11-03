"""Plan files on the artifact volume. The database stores the checksum, not the body.

The relative path is chosen here from the task id and version. Model output never
selects it. A plan file is JSON with `schema_version` 1 and a `ChangePlan`.
"""

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

from patchpilot.llm.plan import ChangePlan
from patchpilot.source.paths import is_inside

PLAN_ARTIFACT_TYPE = "plan"
PLAN_SCHEMA_VERSION = 1
MAX_PLAN_BYTES = 32_768
MAX_PLAN_VERSION = 1000

_PLAN_FILE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/plan/v([1-9][0-9]{0,3})\.json$"
)


class ArtifactStoreError(Exception):
    """The plan file could not be written or did not match its stored checksum."""


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


def plan_relative_path(task_id: UUID, version: int) -> str:
    """Internal path for one plan revision. It is not taken from the model."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise ArtifactStoreError
    if not 1 <= version <= MAX_PLAN_VERSION:
        raise ArtifactStoreError
    relative = f"{task_id}/plan/v{version}.json"
    if _PLAN_FILE.fullmatch(relative) is None:
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
    if len(payload) > MAX_PLAN_BYTES:
        raise ArtifactStoreError
    path = _destination(root, relative_path, create=True)
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
    path = _destination(root, relative_path, create=False)
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


def _destination(root: Path, relative_path: str, *, create: bool) -> Path:
    if _PLAN_FILE.fullmatch(relative_path) is None:
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

"""Which paths a patch may change.

A path must be one of the approved plan modifications, and that modification
must still be an existing `.py` file or a new `tests/test_*.py` file inside the
worktree. Symlinks, secrets, `.git`, CI workflows, Docker files, dependency
manifests, and every other name are refused. Nothing here writes.
"""

from pathlib import Path

from patchpilot.agents.plan_paths import modification_is_allowed
from patchpilot.llm.plan import ChangePlan, ModificationKind, ProposedModification
from patchpilot.patching.errors import PatchError
from patchpilot.source.errors import SourceError
from patchpilot.source.paths import relative_parts

_DOCKER_NAMES = frozenset(
    {
        "dockerfile",
        "containerfile",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
)
_DEPENDENCY_NAMES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
    }
)
_CI_SEGMENTS = frozenset({".github", ".gitlab", ".circleci", "workflows"})


def approved_modification(plan: ChangePlan, relative_path: str) -> ProposedModification:
    """Return the plan entry for this path, or raise when the patch must be refused."""
    canonical = _canonical(relative_path)
    if canonical is None or _blocked(canonical):
        raise PatchError("denied_path")
    matches = [
        item
        for item in plan.modifications
        if _canonical(item.relative_path) == canonical and not _blocked(item.relative_path)
    ]
    if not matches:
        raise PatchError("outside_plan")
    return matches[0]


def path_is_writable(worktree: Path, modification: ProposedModification) -> bool:
    """True when this approved path is still a normal text target in the worktree."""
    if not isinstance(worktree, Path):
        return False
    if not isinstance(modification.kind, ModificationKind):
        return False
    canonical = _canonical(modification.relative_path)
    if canonical is None or _blocked(canonical):
        return False
    return modification_is_allowed(worktree, canonical, modification.kind)


def _canonical(relative_path: str) -> str | None:
    try:
        parts = relative_parts(relative_path, allow_root=False)
    except SourceError:
        return None
    if not parts:
        return None
    return "/".join(parts)


def _blocked(relative_path: str) -> bool:
    """Docker, CI, and dependency names are refused even when the plan listed them."""
    lowered = relative_path.casefold()
    segments = lowered.split("/")
    if any(segment in _CI_SEGMENTS or segment in _DOCKER_NAMES for segment in segments):
        return True
    return segments[-1] in _DEPENDENCY_NAMES or segments[-1] in _DOCKER_NAMES

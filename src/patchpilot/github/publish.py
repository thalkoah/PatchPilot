"""Open one draft pull request after a human has approved publication.

The token is never returned. A second call for the same head reuses the
existing draft instead of opening another.
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx2
from pydantic import SecretStr

from patchpilot.github.body import pull_request_body, pull_request_title
from patchpilot.github.client import DraftPullRequest, GitHubClient
from patchpilot.github.errors import GitHubError
from patchpilot.github.names import GitHubRepo, repo_from_registration
from patchpilot.github.push import push_task_branch


@dataclass(frozen=True, slots=True)
class GitHubOptions:
    """Process settings for publication. `local_remote` is only for tests."""

    token: SecretStr
    allowed: frozenset[str]
    transport: httpx2.BaseTransport | None = None
    local_remote: Path | None = None


@dataclass(frozen=True, slots=True)
class GitHubBinding:
    """The one repository this task may publish to. It is not model output."""

    repo: GitHubRepo
    token: SecretStr
    transport: httpx2.BaseTransport | None = None
    local_remote: Path | None = None


def binding_for(registration: object, options: GitHubOptions) -> GitHubBinding | None:
    """Bind a registry row to the allowlist. `None` means do not publish."""
    repo = repo_from_registration(registration, allowed=options.allowed)
    if repo is None:
        return None
    return GitHubBinding(
        repo=repo,
        token=options.token,
        transport=options.transport,
        local_remote=options.local_remote,
    )


def publish_draft(
    *,
    task_id: UUID,
    worktree: Path,
    repo: GitHubRepo,
    token: SecretStr | None,
    issue_title: str,
    description: str,
    reasoning: str,
    tests_passed: bool,
    tests_summary: str,
    transport: httpx2.BaseTransport | None = None,
    local_remote: Path | None = None,
) -> DraftPullRequest:
    """Push the task branch, then open or reuse one draft pull request.

    A missing token raises before any Git command or HTTP request. The compare
    link uses the repository's default branch, which is never the push target.
    """
    if token is None:
        raise GitHubError("token_missing")
    branch = f"patchpilot/{task_id}"
    head = push_task_branch(
        worktree,
        task_id=task_id,
        repo=repo,
        token=token,
        local_remote=local_remote,
    )
    client = GitHubClient(token, transport=transport)
    try:
        base = client.default_branch(repo)
        if base == branch:
            raise GitHubError("branch_refused")
        body = pull_request_body(
            description=description,
            reasoning=reasoning,
            owner=repo.owner,
            name=repo.name,
            branch=branch,
            base=base,
            tests_passed=tests_passed,
            tests_summary=tests_summary,
        )
        if token.get_secret_value() in body or token.get_secret_value() in issue_title:
            raise GitHubError("rejected")
        return client.open_draft(
            repo,
            branch=branch,
            title=pull_request_title(issue_title),
            body=body,
            head_sha=head,
            base=base,
        )
    finally:
        client.close()

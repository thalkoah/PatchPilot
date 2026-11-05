"""GitHub REST calls for one draft pull request.

This client can create and look up a pull request. It has no merge method.
Redirects are not followed, so the token stays on `api.github.com`.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx2
from pydantic import SecretStr

from patchpilot.github.errors import GitHubError
from patchpilot.github.names import GitHubRepo

_API = "https://api.github.com"
_MAX_BODY = 65536
_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class DraftPullRequest:
    """A draft that already exists or was just opened. `draft` is always true."""

    owner: str
    name: str
    number: int
    url: str
    branch: str
    base: str
    head_sha: str
    draft: bool


class GitHubClient:
    """A short-lived client. The caller closes it. The token is a header only."""

    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        secret = _secret(token)
        self._client = httpx2.Client(
            base_url=_API,
            transport=transport,
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "patchpilot",
            },
        )

    def close(self) -> None:
        self._client.close()

    def default_branch(self, repo: GitHubRepo) -> str:
        """The branch a pull request targets. This client never pushes it."""
        return self._default_branch(repo)

    def open_draft(
        self,
        repo: GitHubRepo,
        *,
        branch: str,
        title: str,
        body: str,
        head_sha: str,
        base: str | None = None,
    ) -> DraftPullRequest:
        """Return the draft for this head, creating it only when none exists."""
        base = self._default_branch(repo) if base is None else base
        if base == branch:
            raise GitHubError("branch_refused")
        existing = self._find(repo, branch=branch, base=base, head_sha=head_sha)
        if existing is not None:
            return existing
        created = self._request(
            "POST",
            f"/repos/{repo.owner}/{repo.name}/pulls",
            json={
                "title": title,
                "head": branch,
                "base": base,
                "body": body,
                "draft": True,
            },
        )
        if created.status_code == 422:
            existing = self._find(repo, branch=branch, base=base, head_sha=head_sha)
            if existing is not None:
                return existing
            raise GitHubError("rejected")
        if created.status_code not in {200, 201}:
            _raise_status(created.status_code)
        return _draft(created, repo, branch=branch, base=base, head_sha=head_sha)

    def _default_branch(self, repo: GitHubRepo) -> str:
        response = self._request("GET", f"/repos/{repo.owner}/{repo.name}")
        if response.status_code != 200:
            _raise_status(response.status_code)
        payload = _json_object(response)
        base = payload.get("default_branch")
        if not isinstance(base, str) or base == "" or ".." in base.split("/"):
            raise GitHubError("rejected")
        if len(base) > 100 or any(ord(char) < 32 or char == " " for char in base):
            raise GitHubError("rejected")
        return base

    def _find(
        self,
        repo: GitHubRepo,
        *,
        branch: str,
        base: str,
        head_sha: str,
    ) -> DraftPullRequest | None:
        response = self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/pulls",
            params={"head": f"{repo.owner}:{branch}", "state": "open"},
        )
        if response.status_code != 200:
            _raise_status(response.status_code)
        payload = _json_value(response)
        if not isinstance(payload, list):
            raise GitHubError("rejected")
        matches: list[DraftPullRequest] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                draft = _draft_from_payload(
                    item,
                    repo,
                    branch=branch,
                    base=base,
                    head_sha=head_sha,
                )
            except GitHubError:
                continue
            matches.append(draft)
        if not matches:
            return None
        return min(matches, key=lambda item: item.number)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        if "merge" in path.split("/"):
            raise GitHubError("rejected")
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx2.HTTPError:
            raise GitHubError("network") from None
        if len(response.content) > _MAX_BODY:
            raise GitHubError("rejected")
        return response


def _secret(token: SecretStr | None) -> str:
    if token is None:
        raise GitHubError("token_missing")
    secret = token.get_secret_value()
    if not isinstance(secret, str) or secret.strip() == "":
        raise GitHubError("token_missing")
    if any(char in secret for char in "\r\n\x00"):
        raise GitHubError("token_missing")
    return secret


def _raise_status(status: int) -> None:
    if status in {401, 403}:
        raise GitHubError("unauthorized")
    raise GitHubError("rejected")


def _json_value(response: httpx2.Response) -> object:
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError):
        raise GitHubError("rejected") from None


def _json_object(response: httpx2.Response) -> dict[str, object]:
    payload = _json_value(response)
    if not isinstance(payload, dict):
        raise GitHubError("rejected")
    return payload


def _draft(
    response: httpx2.Response,
    repo: GitHubRepo,
    *,
    branch: str,
    base: str,
    head_sha: str,
) -> DraftPullRequest:
    return _draft_from_payload(
        _json_object(response),
        repo,
        branch=branch,
        base=base,
        head_sha=head_sha,
    )


def _draft_from_payload(
    payload: dict[str, object],
    repo: GitHubRepo,
    *,
    branch: str,
    base: str,
    head_sha: str,
) -> DraftPullRequest:
    number = payload.get("number")
    url = payload.get("html_url")
    draft = payload.get("draft")
    head = payload.get("head")
    base_ref = payload.get("base")
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 1_000_000_000:
        raise GitHubError("rejected")
    if draft is not True:
        raise GitHubError("rejected")
    if url != repo.pull_url(number):
        raise GitHubError("rejected")
    if not isinstance(head, dict) or head.get("ref") != branch or head.get("sha") != head_sha:
        raise GitHubError("rejected")
    if not isinstance(base_ref, dict) or base_ref.get("ref") != base or base == branch:
        raise GitHubError("rejected")
    return DraftPullRequest(
        owner=repo.owner,
        name=repo.name,
        number=number,
        url=url,
        branch=branch,
        base=base,
        head_sha=head_sha,
        draft=True,
    )
